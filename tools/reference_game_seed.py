"""Explicit test-only RNG initialization wrapper; pinned assets stay byte-identical."""

from hashlib import sha256
from pathlib import Path


MAX_INDEX_BYTES = 256 * 1024


def seeded_index(source_path: Path, seed: int | None):
    if seed is None:
        return None, None
    if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF:
        raise ValueError("test seed must be a uint32")
    with (source_path / "index.html").open("rb") as stream:
        original = stream.read(MAX_INDEX_BYTES + 1)
    if len(original) > MAX_INDEX_BYTES or original.count(b"<head>") != 1:
        raise ValueError("seed fixture requires bounded known HTML head")
    script = (
        f"<script>let kadokaTestSeed={seed};Math.random=()=>{{kadokaTestSeed="
        "(1664525*kadokaTestSeed+1013904223)>>>0;return kadokaTestSeed/4294967296;};</script>"
    ).encode("ascii")
    response = original.replace(b"<head>", b"<head>" + script, 1)
    return response, {
        "seed": seed,
        "rng": "uint32 LCG1664525+1013904223; test setup only",
        "pinned_index_sha256": sha256(original).hexdigest(),
        "initialization_script_sha256": sha256(script).hexdigest(),
        "response_wrapper_sha256": sha256(response).hexdigest(),
    }
