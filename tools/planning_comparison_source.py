"""Content fingerprints for immutable developer comparison source cohorts."""

from hashlib import sha256
from pathlib import Path
import subprocess


def source_fingerprint():
    paths = (
        subprocess.check_output(
            [
                "git",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                "src",
                "tools",
            ]
        )
        .decode("utf-8")
        .split("\0")
    )
    digest, count = sha256(), 0
    for name in sorted(path for path in paths if path.endswith(".py")):
        path = Path(name)
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("comparison source must be regular Python files")
        digest.update(name.encode("utf-8") + b"\0" + sha256(path.read_bytes()).digest())
        count += 1
    return {"python_file_count": count, "sha256": digest.hexdigest()}
