"""Check a built Windows bundle without relying on the build machine (Issue #149).

Two commands, both stdlib-only:

``layout <bundle>``
    Static check of ``build/app-bundle/dist/Kadoka``. ``Kadoka.exe`` and
    ``kadoka_native_runtime.dll`` must sit in the bundle root (the native
    discovery code looks next to ``sys.executable``), the PyInstaller runtime
    must sit in ``_internal``, and every DLL that any bundled exe/dll/pyd imports
    (normal and delay-load import tables) must be either present in the bundle
    directories the loader searches, or an operating-system component. VC++
    runtime DLLs (``msvcp140.dll`` and friends) are not OS components: GitHub's
    windows runners and most developer machines have them in System32, so a
    launch test there cannot reveal a missing one. This table check can.

``launch <bundle> --workdir <dir>``
    Copy the bundle to a new path (relocation), then start the copy with a
    minimal environment: PATH holds only Windows directories, no PYTHON* or
    KADOKA_* variables, fresh TEMP/APPDATA/LOCALAPPDATA directories and an
    empty working directory outside the repository. It runs
    ``Kadoka.exe --bundle-self-check`` (the exe reports that its Python path,
    native DLL and assets come from the copy) and ``Kadoka.exe --smoke-test``.

``trust <bundle>``
    Fail-closed inventory of the CA material the bundle carries (Issue #515).
    Every regular file, and every member of every zip archive (including the
    zip appended to tcl90.dll), is checked: a file with a certificate suffix or
    a PEM certificate/PKCS7 marker is decoded with tools/trust_inventory.py and
    fails on an incident certificate (GHSA-6q48-6c7w-9gw2) or when it cannot be
    decoded. ``launch`` also checks what the frozen ``_ssl`` default context
    trusts on this host (the ``trust`` part of the self-check report).

Neither command proves a clean Windows install: the launch still runs on the
host's Windows with whatever it has in System32. See doc/Windows配布bundle説明書.md.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    # Allow `python tools/windows_bundle_check.py` from the repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import trust_inventory  # noqa: E402

EXECUTABLE_NAME = "Kadoka.exe"
NATIVE_LIBRARY_NAME = "kadoka_native_runtime.dll"
INTERNAL_DIR = "_internal"
ASSETS_DIR = Path(INTERNAL_DIR) / "ai_game_player" / "assets"
PE_SUFFIXES = {".exe", ".dll", ".pyd"}
MACHINE_AMD64 = 0x8664
PE32_PLUS_MAGIC = 0x20B
MAX_PE_BYTES = 256 * 1024 * 1024
# Upper bound on descriptors per import table, so a table without its zero
# terminator fails instead of walking the rest of the file.
MAX_DESCRIPTORS = 4096
RELOCATED_NAME = "Kadoka 配布確認"
SELF_CHECK_SCHEMA = "kadoka-bundle-self-check/v1"
OUTPUT_TAIL_CHARS = 2000
# Item names Kadoka.exe --bundle-self-check must report; a report missing one fails.
REQUIRED_SELF_CHECKS = (
    "frozen",
    "import_paths",
    "native_runtime",
    "native_safety",
    "assets",
    "tk",
    "ssl",
    "trust",
)
# Files that hold certificates by convention; one that cannot be decoded as PEM fails.
CERTIFICATE_SUFFIXES = frozenset(
    {".pem", ".crt", ".cer", ".cert", ".der", ".p7b", ".p7c", ".pfx", ".p12"}
)
# PEM labels that carry trust anchors. Only "CERTIFICATE" blocks are decoded; the
# others fail as unsupported instead of being skipped.
PEM_MARKERS = (
    b"-----BEGIN CERTIFICATE-----",
    b"-----BEGIN TRUSTED CERTIFICATE-----",
    b"-----BEGIN X509 CERTIFICATE-----",
    b"-----BEGIN PKCS7-----",
)
SCAN_CHUNK_BYTES = 1024 * 1024
# Bounds for one zip archive, far above the bundle's real zips (base_library.zip has
# about 160 members, tcl90.dll about 840, a few MB in all); beyond them the check fails.
MAX_ZIP_MEMBERS = 20000
MAX_ZIP_TOTAL_BYTES = 512 * 1024 * 1024
# Archive suffixes that must open as zip files; a damaged one fails instead of being skipped.
ZIP_SUFFIXES = frozenset({".zip", ".pyz", ".whl", ".egg", ".jar"})

# DLLs that ship with every supported Windows 10/11 x64 install. Add a name only
# after confirming it is an OS component; a redistributable must be bundled.
OS_PROVIDED_DLLS = frozenset(
    {
        "advapi32.dll",
        "bcrypt.dll",
        "comctl32.dll",
        "comdlg32.dll",
        "crypt32.dll",
        "d3d11.dll",
        "dwmapi.dll",
        "dxgi.dll",
        "gdi32.dll",
        "imm32.dll",
        "iphlpapi.dll",
        "kernel32.dll",
        "msvcrt.dll",
        "netapi32.dll",
        "ntdll.dll",
        "ole32.dll",
        "oleaut32.dll",
        "propsys.dll",
        "psapi.dll",
        "rpcrt4.dll",
        "secur32.dll",
        "setupapi.dll",
        "shell32.dll",
        "shlwapi.dll",
        "ucrtbase.dll",
        "user32.dll",
        "userenv.dll",
        "uxtheme.dll",
        "version.dll",
        "winmm.dll",
        "winspool.drv",
        "ws2_32.dll",
    }
)
VC_RUNTIME_PREFIXES = (
    "msvcp140",
    "vcruntime140",
    "concrt140",
    "vccorlib140",
    "vcomp140",
)
APISET_PREFIXES = ("api-ms-win-", "ext-ms-win-")


@dataclass(frozen=True)
class PeImports:
    """Machine type and imported DLL names of one PE file."""

    machine: int
    optional_magic: int
    imports: tuple[str, ...]
    delay_imports: tuple[str, ...]


class _PeImage:
    """Bounded reader that maps RVAs of one PE image to file offsets."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        if data[:2] != b"MZ" or len(data) < 0x40:
            raise ValueError("not an MZ executable")
        pe_offset = self._u32(0x3C)
        if data[pe_offset : pe_offset + 4] != b"PE\0\0":
            raise ValueError("missing PE signature")
        coff = pe_offset + 4
        self.machine = self._u16(coff)
        section_count = self._u16(coff + 2)
        optional_size = self._u16(coff + 16)
        optional = coff + 20
        magic = self._u16(optional)
        self.optional_magic = magic
        if magic == 0x20B:
            self.image_base = struct.unpack_from("<Q", data, optional + 24)[0]
            directories = optional + 112
        elif magic == 0x10B:
            self.image_base = self._u32(optional + 28)
            directories = optional + 96
        else:
            raise ValueError(f"unknown optional header magic 0x{magic:x}")
        directory_count = self._u32(directories - 4)
        self.directories = [
            struct.unpack_from("<II", data, directories + 8 * index)
            for index in range(min(directory_count, 16))
        ]
        table = optional + optional_size
        self.sections = []
        for index in range(section_count):
            entry = table + 40 * index
            virtual_size, address, raw_size, raw_offset = struct.unpack_from(
                "<IIII", data, entry + 8
            )
            self.sections.append((address, max(virtual_size, raw_size), raw_offset))

    def _u16(self, offset: int) -> int:
        return struct.unpack_from("<H", self.data, offset)[0]

    def _u32(self, offset: int) -> int:
        return struct.unpack_from("<I", self.data, offset)[0]

    def offset(self, rva: int) -> int:
        for address, size, raw_offset in self.sections:
            if address <= rva < address + size:
                return raw_offset + rva - address
        raise ValueError(f"RVA 0x{rva:x} is outside every section")

    def name_at(self, rva: int) -> str:
        start = self.offset(rva)
        end = self.data.find(b"\0", start, start + 512)
        if end < 0:
            raise ValueError("unterminated import name")
        return self.data[start:end].decode("ascii")

    def directory(self, index: int) -> tuple[int, int]:
        return self.directories[index] if index < len(self.directories) else (0, 0)

    def imports(self) -> list[str]:
        # A zero Size with a non-zero RVA is still read: skipping the table would
        # pass a module whose dependencies were never inspected.
        rva, _size = self.directory(1)
        names: list[str] = []
        if not rva:
            return names
        cursor = self.offset(rva)
        for _ in range(MAX_DESCRIPTORS):
            fields = struct.unpack_from("<IIIII", self.data, cursor)
            if not any(fields):
                return names
            names.append(self.name_at(fields[3]))
            cursor += 20
        raise ValueError(f"import table has more than {MAX_DESCRIPTORS} entries")

    def delay_imports(self) -> list[str]:
        rva, _size = self.directory(13)
        names: list[str] = []
        if not rva:
            return names
        cursor = self.offset(rva)
        for _ in range(MAX_DESCRIPTORS):
            fields = struct.unpack_from("<IIIIIIII", self.data, cursor)
            if not any(fields):
                return names
            attributes, name = fields[0], fields[1]
            # Attribute bit 0 clear means the old VA-based descriptor format.
            names.append(
                self.name_at(name if attributes & 1 else name - self.image_base)
            )
            cursor += 32
        raise ValueError(f"delay-load table has more than {MAX_DESCRIPTORS} entries")


def read_pe_imports(path: Path) -> PeImports:
    """Return the machine type and import tables of a PE file; ValueError if malformed."""
    with path.open("rb") as stream:
        data = stream.read(MAX_PE_BYTES + 1)
    if len(data) > MAX_PE_BYTES:
        raise ValueError(f"larger than {MAX_PE_BYTES} bytes")
    try:
        image = _PeImage(data)
        return PeImports(
            image.machine,
            image.optional_magic,
            tuple(image.imports()),
            tuple(image.delay_imports()),
        )
    except struct.error as error:
        raise ValueError(f"truncated PE structure: {error}") from error


def is_os_component(name: str) -> bool:
    """Whether Windows itself provides `name` (API set contracts and OS_PROVIDED_DLLS)."""
    lowered = name.lower()
    return lowered.startswith(APISET_PREFIXES) or lowered in OS_PROVIDED_DLLS


def _search_dirs(module: Path, bundle: Path) -> list[Path]:
    # LoadLibraryEx with LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | DEFAULT_DIRS (what
    # runtime/native.py uses) searches the module's directory, the application
    # directory and directories PyInstaller's bootloader registers (_internal).
    return list(dict.fromkeys([module.parent, bundle, bundle / INTERNAL_DIR]))


def _names_in(directory: Path, cache: dict[Path, set[str]]) -> set[str]:
    if directory not in cache:
        cache[directory] = (
            {entry.name.lower() for entry in directory.iterdir() if entry.is_file()}
            if directory.is_dir()
            else set()
        )
    return cache[directory]


def check_layout(bundle: Path) -> list[str]:
    """Return every reason `bundle` cannot start without host-installed DLLs (empty = pass)."""
    bundle = bundle.resolve()
    errors: list[str] = []
    for required in (EXECUTABLE_NAME, NATIVE_LIBRARY_NAME):
        if not (bundle / required).is_file():
            errors.append(f"{required} must be in the bundle root {bundle}")
    internal = bundle / INTERNAL_DIR
    runtimes = sorted(internal.glob("python3*.dll")) if internal.is_dir() else []
    if len(runtimes) != 1:
        errors.append(
            f"expected exactly one python3*.dll in {internal}, found {len(runtimes)}"
        )
    assets = bundle / ASSETS_DIR
    if not assets.is_dir() or not any(path.is_file() for path in assets.rglob("*")):
        errors.append(f"bundled assets are missing or empty: {assets}")
    modules = sorted(
        path
        for path in bundle.rglob("*")
        if path.is_file() and path.suffix.lower() in PE_SUFFIXES
    )
    if not modules:
        errors.append(f"no PE files under {bundle}")
    cache: dict[Path, set[str]] = {}
    for module in modules:
        relative = module.relative_to(bundle)
        try:
            pe = read_pe_imports(module)
        except (OSError, ValueError) as error:
            errors.append(f"{relative}: cannot read PE imports: {error}")
            continue
        if pe.machine != MACHINE_AMD64 or pe.optional_magic != PE32_PLUS_MAGIC:
            errors.append(
                f"{relative}: machine 0x{pe.machine:04x} / optional header 0x{pe.optional_magic:x}"
                " is not x64 PE32+"
            )
        directories = _search_dirs(module, bundle)
        for kind, names in (("imports", pe.imports), ("delay-loads", pe.delay_imports)):
            for name in names:
                if is_os_component(name):
                    continue
                if any(name.lower() in _names_in(path, cache) for path in directories):
                    continue
                hint = (
                    " (VC++ runtime: bundle it or link the CRT statically; System32 copies on CI do not count)"
                    if name.lower().startswith(VC_RUNTIME_PREFIXES)
                    else ""
                )
                errors.append(
                    f"{relative} {kind} {name}, which is neither bundled nor an OS component{hint}"
                )
    return errors


def _has_pem_marker(path: Path) -> bool:
    """Whether a file contains any PEM_MARKERS, read in chunks so large PE files stay bounded."""
    overlap = max(len(marker) for marker in PEM_MARKERS) - 1
    tail = b""
    with path.open("rb") as stream:
        while chunk := stream.read(SCAN_CHUNK_BYTES):
            window = tail + chunk
            if any(marker in window for marker in PEM_MARKERS):
                return True
            tail = window[-overlap:]
    return False


def _incident_errors(matches: list[trust_inventory.TargetMatch]) -> list[str]:
    return [
        f"{match.source}: incident certificate {match.sha256} "
        f"({', '.join(match.common_names) or 'no commonName'}; {match.incident})"
        for match in matches
        if match.incident != trust_inventory.NO_INCIDENT_MATCH
    ]


def pem_errors(label: str, data: bytes) -> tuple[str, list[str]]:
    """Decode the PEM certificates in `data` with tools/trust_inventory.py.

    Returns a one-line summary and the reasons the data fails: an incident
    certificate, undecodable base64, a PEM label other than CERTIFICATE, or no
    certificate at all (data that looks like CA material but cannot be
    inspected fails closed).
    """
    digest = hashlib.sha256(data).hexdigest()
    errors = [
        f"{label}: unsupported PEM block {marker.decode('ascii')}"
        for marker in PEM_MARKERS[1:]
        if marker in data
    ]
    try:
        certificates = trust_inventory.read_pem_bundle(data)
    except ValueError as error:
        errors.append(f"{label}: invalid PEM/base64 ({error})")
        return f"{label}: sha256={digest} not decodable", errors
    if not certificates:
        errors.append(f"{label}: looks like CA material but holds no PEM certificate")
    elif data.count(PEM_MARKERS[0]) != len(certificates):
        # read_pem_bundle reads only terminated blocks; an extra BEGIN line is not inspected.
        errors.append(f"{label}: unterminated or malformed CERTIFICATE block")
    errors += _incident_errors(
        trust_inventory.scan_der_certificates(label, certificates)
    )
    return f"{label}: {len(certificates)} certificates sha256={digest}", errors


def _looks_like_ca(name: str, data: bytes) -> bool:
    return Path(name).suffix.lower() in CERTIFICATE_SUFFIXES or any(
        marker in data for marker in PEM_MARKERS
    )


def _zip_member_findings(relative: str, path: Path) -> tuple[list[str], list[str]]:
    found: list[str] = []
    errors: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            total = sum(member.file_size for member in members)
            if len(members) > MAX_ZIP_MEMBERS or total > MAX_ZIP_TOTAL_BYTES:
                return found, [
                    f"{relative}: zip archive too large to inspect "
                    f"({len(members)} members, {total} bytes)"
                ]
            for member in members:
                if member.is_dir():
                    continue
                label = f"{relative}!{member.filename}"
                if member.file_size > trust_inventory.MAX_BUNDLE_BYTES:
                    errors.append(f"{label}: member too large to inspect")
                    continue
                data = archive.read(member)
                if _looks_like_ca(member.filename, data):
                    summary, member_errors = pem_errors(label, data)
                    found.append(summary)
                    errors += member_errors
    except (OSError, zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
        errors.append(
            f"{relative}: zip archive not inspected ({type(error).__name__}: {error})"
        )
    return found, errors


def check_carried_trust(bundle: Path) -> tuple[list[str], list[str]]:
    """List the CA material `bundle` carries and every reason it fails (Issue #515).

    Every regular file is checked for a certificate suffix or a PEM marker, and
    every zip archive (base_library.zip, the zip appended to tcl90.dll) is opened
    and its members are checked the same way. Compressed data that is not a zip
    member, such as the PYZ archive embedded in Kadoka.exe, is not decompressed;
    the self-check reports which trust modules that archive provides.
    """
    bundle = bundle.resolve()
    found: list[str] = []
    errors: list[str] = []
    if not bundle.is_dir():
        return found, [f"bundle directory not found: {bundle}"]
    for path in sorted(bundle.rglob("*")):
        relative = path.relative_to(bundle).as_posix()
        if path.is_symlink():
            errors.append(f"{relative}: symbolic link not inspected")
            continue
        if not path.is_file():
            continue
        try:
            if path.suffix.lower() in CERTIFICATE_SUFFIXES or _has_pem_marker(path):
                if path.stat().st_size > trust_inventory.MAX_BUNDLE_BYTES:
                    errors.append(f"{relative}: CA material too large to inspect")
                else:
                    summary, file_errors = pem_errors(relative, path.read_bytes())
                    found.append(summary)
                    errors += file_errors
            if zipfile.is_zipfile(path) or path.suffix.lower() in ZIP_SUFFIXES:
                members, member_errors = _zip_member_findings(relative, path)
                found += members
                errors += member_errors
        except OSError as error:
            errors.append(f"{relative}: unreadable ({type(error).__name__})")
    return found, errors


def _host_trust_file(label: str, path: Path) -> tuple[str, list[str]]:
    result = trust_inventory.inspect_bundle_file(path)
    if result.status != "inspected":
        return f"{label}: {path}: {result.status}", [
            f"{label}: {path}: {result.status}"
        ]
    return (
        f"{label}: {path}: {result.certificate_count} certificates sha256={result.sha256}",
        [f"{label}: {error}" for error in _incident_errors(result.matches or [])],
    )


def trust_report_errors(trust: object) -> tuple[list[str], list[str]]:
    """Summarise and check the self-check's `trust` part: what the frozen _ssl trusts here.

    The default context's CA list (on Windows the ROOT and CA stores, plus the
    OpenSSL default CA file when it exists) and the raw Windows CA/ROOT store
    entries (get_ca_certs omits non-CA certificates) are decoded and matched
    against the incident; on Windows the store list is required. The OpenSSL default CA file and every file in the default CA
    directory, when they exist on this host, are read too (the directory is
    loaded lazily and is not in the CA list). Anything malformed or unreadable
    fails.
    """
    if not isinstance(trust, dict):
        return [], [f"self-check report has no trust object: {trust!r:.200}"]
    lines: list[str] = []
    errors: list[str] = []
    encoded = trust.get("context_ca_certs")
    if not isinstance(encoded, list):
        errors.append("trust.context_ca_certs is not a list")
        encoded = []
    certificates: list[bytes] = []
    for index, text in enumerate(encoded):
        try:
            certificates.append(base64.b64decode(str(text), validate=True))
        except (binascii.Error, ValueError):
            errors.append(f"trust.context_ca_certs[{index}] is not base64")
    matches = trust_inventory.scan_der_certificates(
        "frozen default context", certificates
    )
    lines.append(
        f"frozen default context: {len(certificates)} CA certificates, "
        f"{len(matches)} naming eMudhra/emSign, "
        f"{trust_inventory.incident_count(matches)} incident"
    )
    errors += _incident_errors(matches)
    stores = trust.get("windows_store_certs")
    if trust.get("platform") == "win32" and not (
        isinstance(stores, list)
        and any(
            isinstance(entry, dict) and entry.get("store") == "ROOT" for entry in stores
        )
    ):
        # Windows always has ROOT certificates; an empty list means the stores were not read.
        errors.append("trust.windows_store_certs has no ROOT store entry on Windows")
    store_certificates: dict[str, list[bytes]] = {}
    for index, entry in enumerate(stores if isinstance(stores, list) else []):
        try:
            store = str(entry["store"])
            der = base64.b64decode(str(entry["der"]), validate=True)
        except (TypeError, KeyError, binascii.Error, ValueError):
            errors.append(f"trust.windows_store_certs[{index}] is malformed")
            continue
        store_certificates.setdefault(store, []).append(der)
    for store, ders in sorted(store_certificates.items()):
        store_matches = trust_inventory.scan_der_certificates(f"windows:{store}", ders)
        lines.append(
            f"windows:{store} store (read by the frozen _ssl): {len(ders)} certificates, "
            f"{trust_inventory.incident_count(store_matches)} incident"
        )
        errors += _incident_errors(store_matches)
    paths = trust.get("default_verify_paths")
    if not isinstance(paths, dict):
        errors.append("trust.default_verify_paths is not an object")
        paths = {}
    lines.append(
        "OpenSSL default paths: "
        + ", ".join(f"{key}={paths.get(key)!r}" for key in sorted(paths))
    )
    # get_default_verify_paths reports cafile/capath only when they exist, so a
    # reported path that cannot be read now fails instead of being skipped.
    cafile, capath = paths.get("cafile"), paths.get("capath")
    if cafile is not None:
        line, file_errors = _host_trust_file("default cafile", Path(str(cafile)))
        lines.append(line)
        errors += file_errors
    if capath is not None and not Path(str(capath)).is_dir():
        errors.append(
            f"default capath: {capath}: reported but not a readable directory"
        )
    elif capath is not None:
        for entry in sorted(Path(str(capath)).iterdir()):
            if entry.is_file():
                line, file_errors = _host_trust_file("default capath", entry)
                lines.append(line)
                errors += file_errors
    lines.append(f"trust modules in the frozen app: {trust.get('modules')!r}")
    return lines, errors


def minimal_environment(workdir: Path) -> dict[str, str]:
    """Environment for a launch that cannot see the developer's PATH, Python or profile."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    profile = workdir / "profile"
    environment = {
        "SystemRoot": system_root,
        "windir": system_root,
        "SystemDrive": os.environ.get("SystemDrive", "C:"),
        # The child is a Windows process, so join with ";" whatever the host is.
        "PATH": ";".join(
            [
                str(Path(system_root) / "System32"),
                system_root,
                str(Path(system_root) / "System32" / "Wbem"),
            ]
        ),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "COMPUTERNAME": os.environ.get("COMPUTERNAME", "kadoka-smoke"),
        "USERNAME": os.environ.get("USERNAME", "kadoka"),
        "USERPROFILE": str(profile),
        "APPDATA": str(profile / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(profile / "AppData" / "Local"),
        "TEMP": str(profile / "Temp"),
        "TMP": str(profile / "Temp"),
    }
    for key in ("APPDATA", "LOCALAPPDATA", "TEMP"):
        Path(environment[key]).mkdir(parents=True, exist_ok=True)
    return environment


def _run(
    executable: Path,
    arguments: list[str],
    environment: dict[str, str],
    cwd: Path,
    timeout: float,
) -> tuple[int | str, str]:
    """Exit code (or why the run did not finish) and the last output lines of one exe run."""
    try:
        completed = subprocess.run(
            [str(executable), *arguments],
            cwd=cwd,
            env=environment,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout:g}s", ""
    except OSError as error:
        return f"could not start: {error}", ""
    # A windowed exe normally prints nothing; keep whatever it did print for diagnosis.
    output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
    return completed.returncode, output[-OUTPUT_TAIL_CHARS:]


def _failure(label: str, code: int | str, output: str) -> str:
    return f"{label} result {code!r}" + (
        f"; output tail: {output.strip()}" if output.strip() else ""
    )


def report_errors(report: object, code: int | str, relocated: Path) -> list[str]:
    """Reasons a --bundle-self-check report (parsed JSON) and exit code do not show a pass."""
    if not isinstance(report, dict) or not isinstance(report.get("checks"), list):
        return [
            f"self-check report is not an object with a checks list: {report!r:.200}"
        ]
    errors: list[str] = []
    checks = report["checks"]
    for index, item in enumerate(checks):
        if not isinstance(item, dict) or not isinstance(item.get("ok"), bool):
            errors.append(f"self-check item {index} is malformed: {item!r:.200}")
        elif not item["ok"]:
            errors.append(f"self-check {item.get('name')}: {item.get('detail')}")
    reported = {item.get("name") for item in checks if isinstance(item, dict)}
    if missing := [name for name in REQUIRED_SELF_CHECKS if name not in reported]:
        errors.append(f"self-check report is missing required checks: {missing}")
    if report.get("schema") != SELF_CHECK_SCHEMA:
        errors.append(f"self-check schema is {report.get('schema')!r}")
    if code != 0 or report.get("ok") is not True or errors:
        errors.append(
            f"--bundle-self-check result {code!r} with ok={report.get('ok')!r}"
        )
    executable_seen = Path(str(report.get("executable", "")))
    if executable_seen.parent != relocated.resolve():
        errors.append(f"self-check ran {executable_seen}, not the relocated copy")
    return errors


def launch_relocated(bundle: Path, workdir: Path, timeout: float = 120.0) -> list[str]:
    """Copy `bundle` under `workdir`, start the copy with minimal_environment and return failures."""
    workdir = workdir.resolve()
    if workdir.exists() and any(workdir.iterdir()):
        return [f"workdir must be new or empty: {workdir}"]
    workdir.mkdir(parents=True, exist_ok=True)
    relocated = workdir / RELOCATED_NAME
    shutil.copytree(bundle, relocated)
    cwd = workdir / "cwd"
    cwd.mkdir()
    environment = minimal_environment(workdir)
    executable = relocated / EXECUTABLE_NAME
    report_path = workdir / "bundle-self-check.json"
    errors: list[str] = []
    code, output = _run(
        executable, ["--bundle-self-check", str(report_path)], environment, cwd, timeout
    )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        errors.append(
            _failure("--bundle-self-check", code, output)
            + f"; no readable report: {error}"
        )
    else:
        trust = report.get("trust") if isinstance(report, dict) else None
        # The CA list is long and already summarised below; print the rest of the report.
        printable = (
            {key: value for key, value in report.items() if key != "trust"}
            if isinstance(report, dict)
            else report
        )
        print(json.dumps(printable, ensure_ascii=False, indent=2))
        errors.extend(report_errors(report, code, relocated))
        lines, trust_errors = trust_report_errors(trust)
        for line in lines:
            print(f"BUNDLE TRUST: {line}")
        errors.extend(trust_errors)
    errors.extend(check_carried_trust(relocated)[1])
    code, output = _run(executable, ["--smoke-test"], environment, cwd, timeout)
    if code != 0:
        errors.append(_failure("--smoke-test", code, output))
    return errors


def main(argv: list[str] | None = None) -> int:
    # Reports name the relocated path, which has Japanese characters; a cp1252
    # console (GitHub's Windows runner) must not turn that into a crash.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    layout = commands.add_parser("layout", help="static layout and DLL import check")
    layout.add_argument("bundle", type=Path)
    launch = commands.add_parser(
        "launch", help="relocate and start with a minimal environment"
    )
    launch.add_argument("bundle", type=Path)
    launch.add_argument(
        "--workdir",
        type=Path,
        required=True,
        help="new or empty directory outside the repository",
    )
    launch.add_argument(
        "--timeout", type=float, default=120.0, help="seconds per exe run"
    )
    trust = commands.add_parser(
        "trust", help="inventory and gate the CA material the bundle carries"
    )
    trust.add_argument("bundle", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.command == "layout":
        errors = check_layout(arguments.bundle)
        label = "BUNDLE LAYOUT"
    elif arguments.command == "trust":
        found, errors = check_carried_trust(arguments.bundle)
        label = "BUNDLE TRUST"
        print(f"{label}: {len(found)} CA files carried in {arguments.bundle}")
        for line in found:
            print(f"{label}: carried {line}")
    else:
        if sys.platform != "win32":
            parser.error("launch requires Windows")
        errors = launch_relocated(
            arguments.bundle, arguments.workdir, arguments.timeout
        )
        label = "BUNDLE LAUNCH"
    for error in errors:
        print(f"{label} ERROR: {error}", file=sys.stderr)
    if errors:
        return 1
    print(f"{label} OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
