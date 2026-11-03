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

Neither command proves a clean Windows install: the launch still runs on the
host's Windows with whatever it has in System32. See doc/Windows配布bundle説明書.md.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

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
)

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
        print(json.dumps(report, ensure_ascii=False, indent=2))
        errors.extend(report_errors(report, code, relocated))
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
    arguments = parser.parse_args(argv)
    if arguments.command == "layout":
        errors = check_layout(arguments.bundle)
        label = "BUNDLE LAYOUT"
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
