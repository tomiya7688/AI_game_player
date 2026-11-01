"""固定owned画像だけでTesseract/modelとnumeric PSMの差を有界に記録する診断。"""

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import struct
import subprocess
import zlib
from io import StringIO
from pathlib import Path
from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame
from tools.planning_score_evaluation import reread_score_header_label
from tools.planning_score_panel import numeric_crops, panel_for_label, prepare_panel_ocr


def png(frame):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    rows = []
    for y in range(frame.height):
        row = frame.bgra[y * frame.width * 4 : (y + 1) * frame.width * 4]
        rows.append(
            b"\0"
            + b"".join(
                row[x + 2 : x + 3] + row[x + 1 : x + 2] + row[x : x + 1]
                for x in range(0, len(row), 4)
            )
        )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(
            b"IHDR", struct.pack(">IIBBBBB", frame.width, frame.height, 8, 2, 0, 0, 0)
        )
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


def digest(path, limit):
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return {"status": "unavailable"}
        return {
            "status": "available",
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    except OSError:
        return {"status": "unavailable"}


class Ocr:
    def __init__(self, executable):
        self.executable = executable

    def raw(self, data, mode):
        command = [self.executable, "stdin", "stdout", "--psm", str(mode)]
        if mode != 6:
            command += ["-c", "tessedit_char_whitelist=0123456789,"]
        run = subprocess.run(
            command + ["tsv"],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=10,
        )
        if len(run.stdout) > 65536:
            raise ValueError("OCR diagnostic output budget exceeded")
        rows = []
        for item in csv.DictReader(
            StringIO(run.stdout.decode("utf-8")), delimiter="\t"
        ):
            if not item["text"].strip():
                continue
            text = item["text"].strip()
            conf = float(item["conf"])
            rows.append(
                {
                    "text": text
                    if text.isascii() and len(text) <= 16
                    else "<bounded-redaction>",
                    "confidence": conf if math.isfinite(conf) else None,
                    "bbox": [int(item[k]) for k in ["left", "top", "width", "height"]],
                }
            )
        return rows[:8]

    def recognize(self, frame, *, mode):
        return [
            dict(
                text=r["text"],
                confidence=r["confidence"] / 100,
                x=r["bbox"][0],
                y=r["bbox"][1],
                width=r["bbox"][2],
                height=r["bbox"][3],
            )
            for r in self.raw(png(frame), 6 if mode == "text_block" else 7)
            if r["confidence"] is not None and r["confidence"] > 0
        ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    executable = shutil.which("tesseract")
    if executable is None:
        raise RuntimeError("Tesseract executable unavailable")
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
        "scope": "owned fixed PNG fixtures, diagnostic only; never an action/score replacement",
        "version": version,
        "executable": digest(Path(executable).resolve(), 128 * 1024 * 1024),
        "eng_traineddata": trained,
        "cases": [],
    }
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}), flush=True)
    folder = Path("tests/fixtures/2048/score-windows-headers")
    manifest = json.loads((folder / "manifest.json").read_text())
    ocr = Ocr(executable)
    seen = set()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for item in manifest["cases"]:
        if item["header_bgra_sha256"] in seen:
            continue
        seen.add(item["header_bgra_sha256"])
        raw = zlib.decompress((folder / item["bgra_zlib"]).read_bytes())
        assert hashlib.sha256(raw).hexdigest() == item["header_bgra_sha256"]
        base = bytearray(b"\xff" * (904 * 648 * 4))
        for y in range(240):
            base[(y * 904 + 202) * 4 : (y * 904 + 842) * 4] = raw[
                y * 640 * 4 : (y + 1) * 640 * 4
            ]
        frame = ScreenFrame(904, 648, bytes(base), captured_at=19)
        header, hd = reread_score_header_label(
            frame,
            ScreenObservation("probe", 904, 648, features={"ocr_candidates": []}),
            (260, 232, 375, 375),
            ocr,
        )
        if header is None:
            record = {
                "image_id": item["image_id"],
                "status": "label_unknown",
                "header": hd,
            }
            report["cases"].append(record)
            print(json.dumps(record), flush=True)
            continue
        label = header.features["ocr_candidates"][0]
        x, y, w, h = label["bbox"]
        panel = panel_for_label(frame, (x, y, w, h))
        top = math.floor(y + h)
        bottom = min(math.ceil(y + h * 5), panel.bottom - 1)
        boxes = numeric_crops(frame, panel, top, bottom)
        for width_index, box in enumerate(boxes):
            prepared, padding = prepare_panel_ocr(frame, box, panel, numeric=True)
            data = png(prepared)
            rgb = b"".join(
                prepared.bgra[i + 2 : i + 3]
                + prepared.bgra[i + 1 : i + 2]
                + prepared.bgra[i : i + 1]
                for i in range(0, len(prepared.bgra), 4)
            )
            path = args.output.parent / (
                item["header_bgra_sha256"][:16] + "-" + str(width_index) + ".png"
            )
            path.write_bytes(data)
            for mode in (7, 8, 10, 13):
                record = {
                    "image_id": item["image_id"],
                    "prepared_png_sha256": hashlib.sha256(data).hexdigest(),
                    "prepared_bgra_sha256": hashlib.sha256(prepared.bgra).hexdigest(),
                    "prepared_rgb_sha256": hashlib.sha256(rgb).hexdigest(),
                    "cropping_profile": "supported_foreground_union_v1",
                    "prepared_size": [prepared.width, prepared.height],
                    "original_roi": box,
                    "padding": padding,
                    "psm": mode,
                    "words": ocr.raw(data, mode),
                }
                report["cases"].append(record)
                print(json.dumps(record), flush=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
