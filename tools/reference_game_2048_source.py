"""Fetch and verify the pinned 2048 source used by Windows E2E runs."""

from __future__ import annotations

import hashlib
import errno
import json
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit


SOURCE_LOCK_PATH = (
    Path(__file__).resolve().parent / "reference_games" / "2048.source.lock.json"
)
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXTRACTED_BYTES = 128 * 1024 * 1024


class PinnedGameSourceError(RuntimeError):
    """Report an invalid, unavailable, or unsafe pinned game source."""


class PinnedGameSource:
    """Materialize one locked game revision without replacing an existing cache."""

    def __init__(self, cache_root: Path, lock_path: Path = SOURCE_LOCK_PATH) -> None:
        self.cache_root = cache_root
        self.lock_path = lock_path
        self.lock = self._read_lock()

    def materialize(self) -> Path:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        commit = str(self.lock["commit"])
        destination = self.cache_root / commit
        if destination.exists():
            self._verify_source(destination)
            return destination

        with tempfile.TemporaryDirectory(
            prefix=f".{commit[:8]}-stage-", dir=self.cache_root
        ) as temporary:
            staging_root = Path(temporary)
            archive_path = staging_root / "source.zip"
            extracted_root = staging_root / "extracted"
            self._download_archive(archive_path)
            self._extract_archive(archive_path, extracted_root)
            self._verify_source(extracted_root)
            try:
                extracted_root.rename(destination)
            except OSError as error:
                # POSIX can report ENOTEMPTY when another process publishes a
                # complete directory first. Only reuse a winner after checking
                # its entire locked source; unrelated rename errors still fail.
                if (
                    not isinstance(error, FileExistsError)
                    and error.errno != errno.ENOTEMPTY
                ):
                    raise
                self._verify_source(destination)
        return destination

    def _read_lock(self) -> dict[str, object]:
        try:
            lock = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PinnedGameSourceError(
                f"2048 source lock could not be read: {self.lock_path}; restore the tracked lock file and retry"
            ) from error
        if (
            not isinstance(lock, dict)
            or lock.get("schema_version") != 1
            or lock.get("game_id") != "2048"
        ):
            raise PinnedGameSourceError(
                "2048 source lock must use schema version 1 and game_id '2048'"
            )
        commit = lock.get("commit")
        repository = lock.get("repository")
        archive_url = lock.get("archive_url")
        git_tree_sha = lock.get("git_tree_sha")
        file_blobs = lock.get("file_blobs")
        license_info = lock.get("license")
        repository_parts = urlsplit(repository) if isinstance(repository, str) else None
        archive_parts = urlsplit(archive_url) if isinstance(archive_url, str) else None
        repository_path = (
            repository_parts.path.strip("/") if repository_parts is not None else ""
        )
        expected_archive_path = (
            f"/{repository_path}/zip/{commit}" if isinstance(commit, str) else ""
        )
        license_sha = (
            license_info.get("git_blob_sha") if isinstance(license_info, dict) else None
        )
        if (
            not isinstance(commit, str)
            or len(commit) != 40
            or any(character not in "0123456789abcdef" for character in commit)
            or repository_parts is None
            or repository_parts.scheme != "https"
            or repository_parts.hostname != "github.com"
            or repository_parts.username is not None
            or repository_parts.password is not None
            or repository_parts.port is not None
            or len(repository_path.split("/")) != 2
            or repository_parts.query
            or repository_parts.fragment
            or archive_parts is None
            or archive_parts.scheme != "https"
            or archive_parts.hostname != "codeload.github.com"
            or archive_parts.path != expected_archive_path
            or archive_parts.query
            or archive_parts.fragment
            or not isinstance(git_tree_sha, str)
            or len(git_tree_sha) != 40
            or any(character not in "0123456789abcdef" for character in git_tree_sha)
            or not isinstance(file_blobs, dict)
            or "index.html" not in file_blobs
            or "LICENSE.txt" not in file_blobs
            or any(
                not isinstance(path, str)
                or not isinstance(blob_sha, str)
                or len(blob_sha) != 40
                or any(character not in "0123456789abcdef" for character in blob_sha)
                or not self._is_safe_locked_path(path)
                for path, blob_sha in file_blobs.items()
            )
            or not isinstance(license_info, dict)
            or license_info.get("spdx_id") != "MIT"
            or license_info.get("path") != "LICENSE.txt"
            or not isinstance(license_sha, str)
            or len(license_sha) != 40
            or any(character not in "0123456789abcdef" for character in license_sha)
            or license_sha != file_blobs.get("LICENSE.txt")
        ):
            raise PinnedGameSourceError(
                "2048 source lock has an invalid commit, archive URL, file list, or MIT license record; "
                "review the tracked lock before retrying"
            )
        return lock

    @staticmethod
    def _is_safe_locked_path(path: str) -> bool:
        posix_path = PurePosixPath(path)
        windows_path = PureWindowsPath(path)
        return (
            bool(path)
            and not posix_path.is_absolute()
            and not windows_path.is_absolute()
            and not windows_path.drive
            and "\\" not in path
            and all(
                part not in {"", ".", ".."} and ":" not in part
                for part in path.split("/")
            )
        )

    def _download_archive(self, archive_path: Path) -> None:
        request = urllib.request.Request(
            str(self.lock["archive_url"]),
            headers={"User-Agent": "Kadoka-AI-Game-Player-E2E"},
        )
        try:
            with (
                urllib.request.urlopen(request, timeout=60) as response,
                archive_path.open("xb") as archive,
            ):
                declared_size = response.headers.get("Content-Length")
                if declared_size and int(declared_size) > MAX_ARCHIVE_BYTES:
                    raise PinnedGameSourceError(
                        "pinned 2048 archive exceeds the 32 MiB download limit; review the commit lock"
                    )
                total_size = 0
                while chunk := response.read(1024 * 128):
                    total_size += len(chunk)
                    if total_size > MAX_ARCHIVE_BYTES:
                        raise PinnedGameSourceError(
                            "pinned 2048 archive exceeds the 32 MiB download limit; review the commit lock"
                        )
                    archive.write(chunk)
        except PinnedGameSourceError:
            raise
        except (OSError, ValueError) as error:
            raise PinnedGameSourceError(
                "pinned 2048 source archive could not be downloaded; check access to codeload.github.com and retry"
            ) from error

    def _extract_archive(self, archive_path: Path, extracted_root: Path) -> None:
        extracted_root.mkdir()
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = archive.infolist()
                if (
                    not members
                    or sum(member.file_size for member in members) > MAX_EXTRACTED_BYTES
                ):
                    raise PinnedGameSourceError(
                        "pinned 2048 archive is empty or exceeds the 128 MiB extraction limit"
                    )
                top_level_names = set()
                for member in members:
                    member_path = self._validated_member_path(member)
                    if member_path.parts:
                        top_level_names.add(member_path.parts[0])
                if len(top_level_names) != 1:
                    raise PinnedGameSourceError(
                        "pinned 2048 archive must contain one source directory"
                    )

                top_level = next(iter(top_level_names))
                seen_files: set[str] = set()
                extracted_bytes = 0
                for member in members:
                    member_path = self._validated_member_path(member)
                    if not member_path.parts or member_path.parts[0] != top_level:
                        continue
                    relative_path = member_path.parts[1:]
                    if not relative_path:
                        continue
                    target = extracted_root.joinpath(*relative_path)
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    normalized_name = "/".join(relative_path).casefold()
                    if normalized_name in seen_files:
                        raise PinnedGameSourceError(
                            "pinned 2048 archive contains duplicate Windows file paths"
                        )
                    seen_files.add(normalized_name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with (
                        archive.open(member) as source,
                        target.open("xb") as destination,
                    ):
                        copied_bytes = 0
                        while chunk := source.read(1024 * 128):
                            copied_bytes += len(chunk)
                            extracted_bytes += len(chunk)
                            if (
                                copied_bytes > member.file_size
                                or extracted_bytes > MAX_EXTRACTED_BYTES
                            ):
                                raise PinnedGameSourceError(
                                    "pinned 2048 archive expanded beyond its declared or configured size"
                                )
                            destination.write(chunk)
                        if copied_bytes != member.file_size:
                            raise PinnedGameSourceError(
                                "pinned 2048 archive member size does not match its directory"
                            )
        except PinnedGameSourceError:
            raise
        except Exception as error:
            raise PinnedGameSourceError(
                "pinned 2048 archive could not be extracted safely; check the locked download and retry"
            ) from error

    @staticmethod
    def _validated_member_path(member: zipfile.ZipInfo) -> PurePosixPath:
        name = member.filename
        windows_path = PureWindowsPath(name)
        path = PurePosixPath(name)
        mode = member.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        raw_parts = name.rstrip("/").split("/")
        device_names = {"con", "prn", "aux", "nul"}
        device_names.update(f"com{number}" for number in range(1, 10))
        device_names.update(f"lpt{number}" for number in range(1, 10))
        if (
            not name
            or "\\" in name
            or "\x00" in name
            or path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or any(
                part in {"", ".", ".."}
                or ":" in part
                or part.endswith((" ", "."))
                or part.split(".")[0].casefold() in device_names
                for part in raw_parts
            )
            or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
        ):
            raise PinnedGameSourceError(
                f"pinned 2048 archive contains an unsafe path: {name!r}"
            )
        return path

    def _verify_source(self, source_root: Path) -> None:
        index_path = source_root / "index.html"
        if (
            source_root.is_symlink()
            or index_path.is_symlink()
            or not index_path.is_file()
        ):
            raise PinnedGameSourceError(
                f"pinned 2048 source is missing index.html: {source_root}"
            )
        file_blobs = self.lock["file_blobs"]
        assert isinstance(file_blobs, dict)
        actual_files: set[str] = set()
        resolved_root = source_root.resolve()
        try:
            for path in source_root.rglob("*"):
                if path.is_symlink():
                    raise PinnedGameSourceError(
                        f"pinned 2048 source cache contains a symbolic link: {path}"
                    )
                try:
                    path.resolve(strict=True).relative_to(resolved_root)
                except ValueError as error:
                    raise PinnedGameSourceError(
                        f"pinned 2048 source cache path escapes its root: {path}"
                    ) from error
                if path.is_file():
                    actual_files.add(path.relative_to(source_root).as_posix())
                elif not path.is_dir():
                    raise PinnedGameSourceError(
                        f"pinned 2048 source cache contains a non-file entry: {path}"
                    )
        except OSError as error:
            raise PinnedGameSourceError(
                f"pinned 2048 source cache could not be inspected: {source_root}"
            ) from error
        if actual_files != set(file_blobs):
            raise PinnedGameSourceError(
                "pinned 2048 source cache has missing or unexpected files; preserve it and review the cache folder"
            )
        for relative_path, expected_blob_sha in file_blobs.items():
            source_file = source_root / PurePosixPath(relative_path)
            try:
                file_bytes = source_file.read_bytes()
            except OSError as error:
                raise PinnedGameSourceError(
                    f"pinned 2048 source file could not be read: {source_file}"
                ) from error
            git_blob_header = f"blob {len(file_bytes)}\0".encode("ascii")
            actual_blob_sha = hashlib.sha1(
                git_blob_header + file_bytes, usedforsecurity=False
            ).hexdigest()
            if actual_blob_sha != expected_blob_sha:
                raise PinnedGameSourceError(
                    f"pinned 2048 source file does not match the lock: {relative_path}; "
                    "preserve the cache and review that file before retrying"
                )
