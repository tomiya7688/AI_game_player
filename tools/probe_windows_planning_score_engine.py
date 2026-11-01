"""固定fixtureの同画素でOCR出力/whitelist/余白整列/階調だけを変える診断。"""

import argparse
from collections import Counter
import csv
from hashlib import sha256
from io import StringIO
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
from unittest.mock import patch
import zlib

from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame
from tools import planning_score_panel
from tools.planning_score_evaluation import reread_score_header_label
from tools.probe_windows_planning_score_ocr import Ocr, digest, png


def pixels_rgb(frame):
    return b"".join(
        frame.bgra[i + 2 : i + 3] + frame.bgra[i + 1 : i + 2] + frame.bgra[i : i + 1]
        for i in range(0, len(frame.bgra), 4)
    )


def width_variant(frame, extra, *, left=False):
    """白い外側列だけを増減し、元glyphは切断しない。"""
    width = frame.width + extra
    if extra < 0:
        if any(
            frame.bgra[(y * frame.width + width) * 4 : (y + 1) * frame.width * 4]
            != b"\xff" * (-extra * 4)
            for y in range(frame.height)
        ):
            raise ValueError("diagnostic trim would remove nonwhite pixels")
    out = bytearray(b"\xff" * (width * frame.height * 4))
    for y in range(frame.height):
        data = frame.bgra[
            y * frame.width * 4 : (y * frame.width + min(width, frame.width)) * 4
        ]
        start = (y * width + (extra if left and extra > 0 else 0)) * 4
        out[start : start + len(data)] = data
    return ScreenFrame(width, frame.height, bytes(out), captured_at=frame.captured_at)


def prepared_variants(frame, box, panel):
    """同じresamplerの最終Otsu直前を観測し、診断だけで階調を保持する。"""
    captures = []
    original = planning_score_panel._otsu

    def capture(pixels):
        captures.append(list(pixels))
        return original(pixels)

    with patch.object(planning_score_panel, "_otsu", side_effect=capture):
        binary, pad = planning_score_panel.prepare_panel_ocr(
            frame, box, panel, numeric=True
        )
    scaled = captures[-1]
    raw_width = (box[2] - box[0]) * 3
    raw_height = (box[3] - box[1]) * 3
    if len(scaled) != raw_width * raw_height:
        raise ValueError("diagnostic intermediate pixel bounds differ")
    background = Counter(scaled).most_common(1)[0][0]
    peak = max(scaled)
    if peak <= background:
        raise ValueError("diagnostic grayscale contrast unavailable")
    raw = bytearray(b"\xff" * (binary.width * binary.height * 4))
    for y in range(raw_height):
        for x in range(raw_width):
            value = round(
                255 * (peak - scaled[y * raw_width + x]) / (peak - background)
            )
            value = min(255, max(0, value))
            offset = ((y + pad) * binary.width + x + pad) * 4
            raw[offset : offset + 4] = bytes((value, value, value, 255))
    grayscale = ScreenFrame(
        binary.width, binary.height, bytes(raw), captured_at=frame.captured_at
    )
    variants = [
        ("binary_reference", binary),
        ("binary_right_plus1", width_variant(binary, 1)),
        ("binary_right_minus1", width_variant(binary, -1)),
        ("binary_left_plus1", width_variant(binary, 1, left=True)),
    ]
    for alignment in (8, 16, 32):
        width = ((binary.width + alignment - 1) // alignment) * alignment
        variants.append(
            (
                "binary_width_align" + str(alignment),
                width_variant(binary, width - binary.width),
            )
        )
    variants.append(("grayscale_same_bilinear", grayscale))
    return variants


def raw_output(executable, data, whitelist, output):
    command = [executable, "stdin", "stdout", "--psm", "7"]
    if whitelist:
        command += ["-c", "tessedit_char_whitelist=0123456789,"]
    if output == "tsv":
        command += ["tsv"]
    result = subprocess.run(
        command,
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=10,
    )
    if len(result.stdout) > 65536:
        raise ValueError("diagnostic OCR output byte budget exceeded")
    text = result.stdout.decode("utf-8")
    if output == "text":
        value = text.strip()
        return {
            "text": value
            if (not value or value.isprintable()) and len(value) <= 32
            else "<bounded-redaction>",
            "empty": not bool(value),
        }
    words = []
    for word in csv.DictReader(StringIO(text), delimiter="\t"):
        if not word["text"].strip():
            continue
        value = word["text"].strip()
        confidence = float(word["conf"])
        words.append(
            {
                "text": value
                if value.isprintable() and len(value) <= 16
                else "<bounded-redaction>",
                "confidence": confidence if math.isfinite(confidence) else None,
                "bbox": [int(word[k]) for k in ("left", "top", "width", "height")],
            }
        )
    return {"words": words[:8]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-id", action="append", required=True)
    args = parser.parse_args()
    if len(args.case_id) > 2 or len(set(args.case_id)) != len(args.case_id):
        raise ValueError("diagnostic accepts at most two explicit fixture IDs")
    executable = shutil.which("tesseract")
    if executable is None:
        raise RuntimeError("Tesseract unavailable")
    version = (
        subprocess.run(
            [executable, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=10,
        )
        .stdout.decode("utf-8", errors="replace")[:8192]
        .splitlines()[0][:128]
    )
    langs = subprocess.run(
        [executable, "--list-langs"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
        timeout=10,
    ).stdout.decode("utf-8", errors="replace")[:8192]
    match = re.search(r'in "([^"\r\n]+)"', langs)
    trained = (
        digest(Path(match[1]) / "eng.traineddata", 64 * 1024 * 1024)
        if match
        else {"status": "unavailable"}
    )
    report = {
        "scope": "explicit owned fixture diagnostic only; shared transforms for all selected cases; no input, score replacement, PSM selection or learning",
        "version": version,
        "diagnostic_script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "executable": digest(Path(executable).resolve(), 128 * 1024 * 1024),
        "eng_traineddata": trained,
        "case_ids": args.case_id,
        "psm": 7,
        "records": [],
    }
    print(json.dumps({k: v for k, v in report.items() if k != "records"}), flush=True)
    folder = Path("tests/fixtures/2048/score-windows-headers")
    manifest = json.loads((folder / "manifest.json").read_text())
    selected = [item for item in manifest["cases"] if item["image_id"] in args.case_id]
    if len(selected) != len(args.case_id):
        raise ValueError("unknown diagnostic fixture ID")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    ocr = Ocr(executable)
    for item in selected:
        raw = zlib.decompress((folder / item["bgra_zlib"]).read_bytes())
        if (
            len(raw) != 640 * 240 * 4
            or sha256(raw).hexdigest() != item["header_bgra_sha256"]
        ):
            raise ValueError("fixture pixel hash mismatch")
        base = bytearray(b"\xff" * (904 * 648 * 4))
        for y in range(240):
            base[(y * 904 + 202) * 4 : (y * 904 + 842) * 4] = raw[
                y * 640 * 4 : (y + 1) * 640 * 4
            ]
        frame = ScreenFrame(904, 648, bytes(base), captured_at=19)
        header, diagnostic = reread_score_header_label(
            frame,
            ScreenObservation("probe", 904, 648, features={"ocr_candidates": []}),
            (260, 232, 375, 375),
            ocr,
        )
        if header is None:
            raise ValueError("literal SCORE diagnostic anchor unavailable")
        box = tuple(header.features["ocr_candidates"][0]["bbox"])
        x, y, w, h = box
        panel = planning_score_panel.panel_for_label(frame, box)
        boxes = planning_score_panel.numeric_crops(
            frame, panel, math.floor(y + h), min(math.ceil(y + h * 5), panel.bottom - 1)
        )
        for crop_index, crop in enumerate(boxes):
            seen = {}
            for variant_name, prepared in prepared_variants(frame, crop, panel):
                data = png(prepared)
                rgb_hash = sha256(pixels_rgb(prepared)).hexdigest()
                alias = seen.get(rgb_hash)
                seen.setdefault(rgb_hash, variant_name)
                path = args.output.parent / (
                    item["header_bgra_sha256"][:16]
                    + "-"
                    + str(crop_index)
                    + "-"
                    + variant_name
                    + ".png"
                )
                path.write_bytes(data)
                for whitelist in (False, True):
                    for output in ("text", "tsv"):
                        record = {
                            "image_id": item["image_id"],
                            "original_roi": crop,
                            "variant": variant_name,
                            "alias_of_identical_pixels": alias,
                            "width": prepared.width,
                            "height": prepared.height,
                            "rgb_sha256": rgb_hash,
                            "png_sha256": sha256(data).hexdigest(),
                            "whitelist": whitelist,
                            "output": output,
                            **raw_output(executable, data, whitelist, output),
                        }
                        report["records"].append(record)
                        print(json.dumps(record), flush=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
