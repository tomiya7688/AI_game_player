"""Inventory Python tooling and certificate trust sources for Issue #513 (parent #482).

The report answers, for each Python interpreter and CA bundle that is inspected:
which pip / certifi / uv / sigstore versions are installed, which CA bundle
`certifi.where()` points to, which certificate settings the environment carries,
and whether certificates naming eMudhra or emSign are present in those trust
sources. It also lists where this repository installs or pins Python tooling.

Read-only behaviour: each interpreter runs a probe script that only imports
modules, reads package metadata, environment variables and certificate stores,
and prints one JSON line. No package is installed or upgraded and no external
command (such as `uv` or `sigstore`) is executed; for those only the path found on
PATH and the installed distribution metadata are recorded. The tool writes only
the report files that are requested.

The probe is not run in isolated mode, because user site-packages and
environment settings are part of what the interpreter really uses. It runs from a
new empty directory so that no module is imported from the caller's working
directory.

Limits of the evidence (also stated in doc/Python証明書信頼棚卸し.md):
- Version checks only compare against the #482 minimums. A version that passes
  does not mean the target certificates were removed; the matches are reported
  separately.
- pip carries its own CA bundle (`pip._vendor.certifi`) and the truststore package.
  The probe reads pip's config files (read-only) and the environment, and
  pip_trust_source() derives the CA source from them. Command-line options of a
  particular pip run are not visible to the probe.
- Target certificates are matched by the names "eMudhra" and "emSign" (ASCII
  case-insensitive, and UTF-16BE for BMPString names) anywhere in a certificate's
  DER bytes. Each match is reported with its SHA-256 fingerprint and commonName and
  classified against the incident advisory (INCIDENT_ADVISORY): a known affected
  root fingerprint, a commonName equal to an affected root's name, or neither.
- On Windows the probe lists what `ssl.enum_certificates` returns for the ROOT, CA
  and Disallowed stores. Windows can download additional roots on demand, so a
  certificate missing from those lists is "not present at inspection time", not
  proof that Windows would reject a chain.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_NAMES = ("eMudhra", "emSign")
# Minimum versions required by the parent incident Issue #482 (body, 2026-10).
MINIMUM_VERSIONS = {
    "pip": "26.2.2",
    "certifi": "2026.10.19",
    "uv": "0.13.4",
    "sigstore": "4.5.1",
}
# The incident behind #482 is GHSA-6q48-6c7w-9gw2: eight intermediates under the
# emSign G3 roots had compromised keys. certifi 2026.10.18 removed every emSign root
# and 2026.10.19 restored G1, C1 and C3 as unaffected (certifi issue #439, PR #440);
# 2026.10.23 later dropped C1 and C3 at eMudhra's own request (Mozilla bug 2083709),
# which is not the incident. Only the fingerprint below was in certifi; the TLS G3
# root never was, so it is matched by name only.
INCIDENT_ADVISORY = "GHSA-6q48-6c7w-9gw2"
INCIDENT_ROOT_FINGERPRINTS = {
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b": "emSign ECC Root CA - G3",
}
# The advisory names ECC G3. The certifi maintainer also named the TLS G3 root as
# affected (issue #439), so it is kept as a precautionary name match.
INCIDENT_ROOT_NAMES = ("emSign ECC Root CA - G3", "emSign Root TLS CA - G3")
# The label for a match that is not on the known incident list. It is not a safety
# verdict: chains are not resolved and the list only holds what was published.
NO_INCIDENT_MATCH = "no known incident match"
# pip enables truststore unconditionally from 24.2 unless "legacy-certs" is requested,
# and only on Python 3.10 or later (truststore's minimum).
PIP_TRUSTSTORE_DEFAULT_FROM = (24, 2)
TRUSTSTORE_MINIMUM_PYTHON = (3, 10)
PROBE_TIMEOUT_SECONDS = 60
WINDOWS_STORES = ("ROOT", "CA", "Disallowed")
PROBE_MARKER = "TRUST_INVENTORY_JSON:"
MAX_LINE_LENGTH = 200
MAX_BUNDLE_BYTES = 20 * 1024 * 1024
_PEM_BLOCK = re.compile(
    rb"-----BEGIN CERTIFICATE-----([^-]*?)-----END CERTIFICATE-----", re.DOTALL
)
# Variables that can change the CA file OpenSSL or requests loads under pip.
CA_OVERRIDE_VARIABLES = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)
CERTIFICATE_ENVIRONMENT_VARIABLES = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "PIP_CERT",
    "PIP_USE_FEATURE",
    "PIP_USE_DEPRECATED",
    "PIP_CONFIG_FILE",
)

# Runs inside the inspected interpreter. It only reads state and prints one marked JSON line.
PROBE_SCRIPT = r"""
import base64, json, os, platform, shutil, ssl, sys
from importlib import metadata

def version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None

def module_file(name):
    try:
        module = __import__(name)
        return getattr(module, "__file__", None)
    except Exception as error:
        return "error: " + type(error).__name__

result = {
    "executable": sys.executable,
    "python_version": platform.python_version(),
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "is_venv": sys.prefix != sys.base_prefix,
    "openssl_version": ssl.OPENSSL_VERSION,
    "ssl_default_verify_paths": ssl.get_default_verify_paths()._asdict(),
    "packages": {name: version(name) for name in ("pip", "certifi", "uv", "sigstore", "truststore")},
    "module_files": {name: module_file(name) for name in ("pip", "certifi")},
    "environment": {name: os.environ.get(name) for name in __ENV_VARS__},
    "cli_paths": {name: shutil.which(name) for name in ("uv", "sigstore")},
}
try:
    import certifi
    result["certifi_where"] = certifi.where()
except Exception as error:
    result["certifi_where"] = None
    result["certifi_error"] = type(error).__name__
try:
    from pip._vendor import certifi as pip_certifi
    result["pip_vendored_certifi"] = {"version": getattr(pip_certifi, "__version__", None), "where": pip_certifi.where()}
except Exception as error:
    result["pip_vendored_certifi"] = {"error": type(error).__name__}
try:
    import pip._vendor.truststore  # noqa: F401
    result["pip_vendored_truststore"] = True
except Exception:
    result["pip_vendored_truststore"] = False
try:
    # Read-only: Configuration.load() only parses the config files pip would use.
    from pip._internal.configuration import Configuration
    configuration = Configuration(isolated=False)
    configuration.load()
    result["pip_config"] = {
        "files": [
            [str(variant), path, os.path.exists(path)]
            for variant, paths in configuration.iter_config_files()
            for path in paths
        ],
        "settings": {
            key: value for key, value in configuration.items()
            if key.rsplit(".", 1)[-1] in ("cert", "client-cert", "use-deprecated", "use-feature")
        },
    }
except Exception as error:
    result["pip_config"] = {"error": type(error).__name__}
windows_store = None
if hasattr(ssl, "enum_certificates"):
    windows_store = []
    for store in __STORES__:
        try:
            for der, encoding, trust in ssl.enum_certificates(store):
                if encoding != "x509_asn":
                    # PKCS#7 entries are not expanded; record them so they are not silently dropped.
                    windows_store.append({"store": store, "error": "not expanded: " + str(encoding)})
                else:
                    windows_store.append({
                        "store": store,
                        "der_b64": base64.b64encode(der).decode(),
                        "trust": sorted(trust) if isinstance(trust, set) else bool(trust),
                    })
        except Exception as error:
            windows_store.append({"store": store, "error": type(error).__name__})
result["windows_store"] = windows_store
print(__MARKER__ + json.dumps(result, ensure_ascii=True))
"""


@dataclass
class TargetMatch:
    """A certificate in a trust source whose DER bytes name a target organisation."""

    source: str
    index: int
    sha256: str
    names: list[str]
    common_names: list[str] = field(default_factory=list)
    trust: object = None
    incident: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "index": self.index,
            "sha256": self.sha256,
            "names": self.names,
            "common_names": self.common_names,
            "trust": self.trust,
            "incident": self.incident,
        }


@dataclass
class BundleResult:
    """One CA bundle file: inspected with its matches, or why it could not be inspected."""

    path: str
    status: str
    sha256: str | None = None
    certificate_count: int | None = None
    matches: list[TargetMatch] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "status": self.status,
            "sha256": self.sha256,
            "certificate_count": self.certificate_count,
            "target_matches": None
            if self.matches is None
            else [m.to_dict() for m in self.matches],
        }


@dataclass
class EnvironmentReport:
    """What one inspected interpreter or bundle uses, or why it could not be inspected."""

    label: str
    status: str
    details: dict[str, object] = field(default_factory=dict)
    version_checks: dict[str, str] = field(default_factory=dict)
    certifi_bundle: BundleResult | None = None
    pip_bundle: BundleResult | None = None
    windows_store_matches: list[TargetMatch] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "status": self.status,
            "details": self.details,
            "version_checks": self.version_checks,
            "certifi_bundle": self.certifi_bundle.to_dict()
            if self.certifi_bundle
            else None,
            "pip_vendored_bundle": self.pip_bundle.to_dict()
            if self.pip_bundle
            else None,
            "windows_store_target_matches": None
            if self.windows_store_matches is None
            else [m.to_dict() for m in self.windows_store_matches],
        }


def _release_tuple(text: str) -> tuple[int, ...]:
    parts = [int(part) for part in text.split(".")]
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def check_version(name: str, installed: str | None) -> str:
    """Classify an installed version against the #482 minimum.

    Returns "ok", "outdated", "not installed" or "unknown". Release numbers are
    compared with trailing zeros ignored (26.2 == 26.2.0). A pre-release or other
    suffix after the release number is reported as "unknown" instead of guessed.
    """
    if installed is None:
        return "not installed"
    found = re.search(r"(\d+(?:\.\d+)*)([^\s]*)", installed)
    if not found:
        return f"unknown ({installed})"
    release, suffix = found.group(1), found.group(2)
    minimum = MINIMUM_VERSIONS[name]
    if suffix and not re.fullmatch(r"\.?post\d*", suffix):
        return f"unknown ({installed}; pre-release or local suffix)"
    if _release_tuple(release) >= _release_tuple(minimum):
        return f"ok ({release} >= {minimum})"
    return f"outdated ({release} < {minimum})"


_COMMON_NAME_OID = b"\x06\x03\x55\x04\x03"  # OID 2.5.4.3 (commonName), DER encoded
# UTF8String, PrintableString, TeletexString, IA5String, BMPString
_STRING_TAGS = {0x0C, 0x13, 0x14, 0x16, 0x1E}


def common_names(der: bytes) -> list[str]:
    """Return the commonName values in a certificate (issuer first, then subject), best effort.

    The OID is located by a byte search, not by a full X.509 parse, and only
    short-form DER lengths are decoded; anything else is skipped rather than guessed.
    """
    names: list[str] = []
    position = der.find(_COMMON_NAME_OID)
    while position != -1:
        value_at = position + len(_COMMON_NAME_OID)
        if (
            value_at + 2 <= len(der)
            and der[value_at] in _STRING_TAGS
            and der[value_at + 1] < 0x80
        ):
            length = der[value_at + 1]
            if value_at + 2 + length > len(der):
                position = der.find(_COMMON_NAME_OID, value_at)
                continue
            raw = der[value_at + 2 : value_at + 2 + length]
            encoding = "utf-16-be" if der[value_at] == 0x1E else "utf-8"
            if raw:
                names.append(raw.decode(encoding, errors="replace"))
        position = der.find(_COMMON_NAME_OID, value_at)
    return list(dict.fromkeys(names))


def _target_names(der: bytes) -> list[str]:
    lowered = der.lower()
    found = []
    for name in TARGET_NAMES:
        if (
            name.lower().encode("ascii") in lowered
            or name.lower().encode("utf-16-be") in lowered
        ):
            found.append(name)
    return found


def classify_incident(sha256: str, names: list[str]) -> str:
    """Say whether a matched certificate belongs to the GHSA-6q48-6c7w-9gw2 incident.

    "incident root" is a known fingerprint of an affected root. "incident-related
    name" means a commonName (issuer or subject) equals an affected root's name,
    ignoring case, spacing and dash variants, so the certificate is that root under
    another fingerprint or is issued directly by it, such as a compromised
    intermediate. Anything else is NO_INCIDENT_MATCH, which does not mean safe: a
    certificate further down a chain, or named differently, is not recognised.
    """
    if sha256 in INCIDENT_ROOT_FINGERPRINTS:
        return f"incident root ({INCIDENT_ADVISORY})"
    incident_names = {_normalised_name(name) for name in INCIDENT_ROOT_NAMES}
    if any(_normalised_name(name) in incident_names for name in names):
        return f"incident-related name ({INCIDENT_ADVISORY})"
    return NO_INCIDENT_MATCH


def _normalised_name(name: str) -> str:
    """Fold case, dash variants and spacing so equal names written differently compare equal."""
    dashes = re.sub("[\u2010-\u2015\u2212]", "-", name.casefold())
    return " ".join(dashes.replace("-", " - ").split())


def scan_der_certificates(source: str, certificates: list[bytes]) -> list[TargetMatch]:
    """Return the certificates whose DER bytes contain a target organisation name."""
    matches = []
    for index, der in enumerate(certificates):
        names = _target_names(der)
        if names:
            sha256 = hashlib.sha256(der).hexdigest()
            cns = common_names(der)
            matches.append(
                TargetMatch(
                    source,
                    index,
                    sha256,
                    names,
                    cns,
                    incident=classify_incident(sha256, cns),
                )
            )
    return matches


def incident_count(matches: list[TargetMatch] | None) -> int:
    """Count the matches that belong to the incident, so a report can show them apart."""
    return sum(1 for match in matches or [] if match.incident != NO_INCIDENT_MATCH)


def _release_or_none(text: object) -> tuple[int, ...] | None:
    found = re.match(r"\d+(?:\.\d+)*", text) if isinstance(text, str) else None
    return _release_tuple(found.group(0)) if found else None


def pip_trust_source(details: dict[str, object]) -> str:
    """Describe the CA source pip uses for HTTPS, from what the probe recorded.

    From pip 24.2 on Python 3.10+, pip verifies through its vendored truststore,
    which checks the OS trust store and also loads pip's vendored certifi bundle (on
    Windows the bundle is tried when the OS store rejects the chain; elsewhere both
    are loaded into one OpenSSL context), unless "legacy-certs" is set in
    PIP_USE_DEPRECATED, or else by the global use-deprecated config setting (the
    environment overrides config files). When pip's config could not be read, or a
    cert option, a command-specific setting or a CA environment variable is set,
    the result starts with "unknown" and the default source is given only as what
    applies without those settings.
    """
    packages = _dict(details.get("packages"))
    if not packages.get("pip"):
        return "pip not installed"
    release = _release_or_none(packages.get("pip"))
    config = _dict(details.get("pip_config"))
    settings = _dict(config.get("settings"))
    environment = _dict(details.get("environment"))
    # pip lets environment variables override config files, and only the "global"
    # section applies to every command; other sections apply to one command only.
    if environment.get("PIP_USE_DEPRECATED"):
        deprecated = [str(environment["PIP_USE_DEPRECATED"])]
    else:
        deprecated = [
            str(value)
            for key, value in settings.items()
            if key == "global.use-deprecated"
        ]
    scoped = sorted(
        key
        for key, value in settings.items()
        if value
        and not key.startswith("global.")
        and key.rsplit(".", 1)[-1] in ("cert", "use-deprecated")
    )
    cert = [key for key, value in settings.items() if key == "global.cert" and value]
    if environment.get("PIP_CERT"):
        cert.append("PIP_CERT")
    ca_overrides = [name for name in CA_OVERRIDE_VARIABLES if environment.get(name)]
    python = _release_or_none(details.get("python_version"))
    if release is None:
        return f"unknown (pip version {packages.get('pip')} not parsed)"
    if release < PIP_TRUSTSTORE_DEFAULT_FROM:
        source = (
            "pip vendored certifi only (pip < 24.2 does not use truststore by default)"
        )
    elif python is not None and python[:2] < TRUSTSTORE_MINIMUM_PYTHON:
        source = "pip vendored certifi only (Python < 3.10 cannot use truststore)"
    elif any("legacy-certs" in value for value in deprecated):
        source = "pip vendored certifi only (legacy-certs requested)"
    elif details.get("pip_vendored_truststore") is not True:
        source = "pip vendored certifi only (vendored truststore missing)"
    else:
        source = "OS trust store via truststore, plus the pip vendored certifi bundle"
    unknowns = []
    if python is None:
        unknowns.append("Python version not recorded")
    if "pip_config" not in details:
        unknowns.append("pip config not recorded")
    elif "error" in config:
        unknowns.append(f"pip config not read ({config['error']})")
    if cert:
        unknowns.append(f"cert option set ({', '.join(cert)}), not analysed")
    if scoped:
        unknowns.append(f"command-specific settings ({', '.join(scoped)})")
    if ca_overrides:
        unknowns.append(
            f"CA environment variables set ({', '.join(ca_overrides)}), not analysed"
        )
    if unknowns:
        return f"unknown ({'; '.join(unknowns)}); without them: {source}"
    return source


def read_pem_bundle(data: bytes) -> list[bytes]:
    """Decode every PEM certificate block in CA bundle bytes into DER bytes.

    Raises ValueError (binascii.Error is a subclass) when a block is not valid
    base64 or decodes to no bytes.
    """
    certificates = []
    for block in _PEM_BLOCK.findall(data):
        der = base64.b64decode(b"".join(block.split()), validate=True)
        if not der:
            raise ValueError("empty certificate block")
        certificates.append(der)
    return certificates


def inspect_bundle_file(path: Path) -> BundleResult:
    """Read a CA bundle once and report its fingerprint, size and target matches.

    The path may come from the inspected interpreter, so only absolute paths to
    regular files are read, and at most MAX_BUNDLE_BYTES are read even if the file
    grows after it was checked (a pipe or device path could block or be huge).
    """
    if not path.is_absolute():
        return BundleResult(str(path), "not inspected: not an absolute path")
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return BundleResult(str(path), "not inspected: not a regular file")
        with path.open("rb") as stream:
            data = stream.read(MAX_BUNDLE_BYTES + 1)
    except OSError as error:
        return BundleResult(
            str(path), f"not inspected: unreadable ({type(error).__name__})"
        )
    if len(data) > MAX_BUNDLE_BYTES:
        return BundleResult(
            str(path), f"not inspected: larger than {MAX_BUNDLE_BYTES} bytes"
        )
    digest = hashlib.sha256(data).hexdigest()
    try:
        certificates = read_pem_bundle(data)
    except ValueError:
        return BundleResult(str(path), "not inspected: invalid PEM/base64", digest)
    if not certificates:
        return BundleResult(
            str(path), "not inspected: no PEM certificates found", digest, 0
        )
    return BundleResult(
        str(path),
        "inspected",
        digest,
        len(certificates),
        scan_der_certificates(str(path), certificates),
    )


def inspect_bundle(path: Path, label: str | None = None) -> EnvironmentReport:
    """Inspect a CA bundle file on its own, for bundles that are not reached through an interpreter."""
    bundle = inspect_bundle_file(path.resolve())
    return EnvironmentReport(
        label or str(path),
        bundle.status,
        {"bundle_path": str(path)},
        certifi_bundle=bundle,
    )


def _probe_command(python: str) -> list[str]:
    script = (
        PROBE_SCRIPT.replace("__ENV_VARS__", repr(CERTIFICATE_ENVIRONMENT_VARIABLES))
        .replace("__STORES__", repr(WINDOWS_STORES))
        .replace("__MARKER__", repr(PROBE_MARKER))
    )
    # A relative path would otherwise be resolved against the empty working directory.
    candidate = Path(python)
    if not candidate.is_absolute() and len(candidate.parts) > 1 and candidate.exists():
        python = str(candidate.resolve())
    return [python, "-c", script]


def _parse_probe_output(stdout: str) -> dict[str, object] | None:
    """Return the probe's JSON object from its marked line, ignoring other output (e.g. sitecustomize)."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(PROBE_MARKER):
            try:
                value = json.loads(line[len(PROBE_MARKER) :])
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) else None
    return None


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _trust_value(value: object) -> object:
    """Keep the Windows trust setting only in its documented shapes: True/False or a list of OIDs."""
    if isinstance(value, bool):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return None


def _windows_store_matches(report: EnvironmentReport, entries: object) -> None:
    if not isinstance(entries, list):
        report.details["windows_store_error"] = (
            "probe returned an unexpected store listing"
        )
        return
    ders: list[bytes] = []
    stores: list[str] = []
    trusts: list[object] = []
    counts: dict[str, int] = dict.fromkeys(WINDOWS_STORES, 0)
    errors = []
    for entry in entries:
        entry = _dict(entry)
        store = str(entry.get("store", "?"))
        if "der_b64" not in entry:
            errors.append(
                {
                    "store": store,
                    "error": str(entry.get("error", "missing certificate")),
                }
            )
            continue
        try:
            der = base64.b64decode(str(entry["der_b64"]), validate=True)
        except ValueError:
            errors.append({"store": store, "error": "invalid base64"})
            continue
        # The three lists stay aligned with the DER list, so match.index selects the same entry.
        ders.append(der)
        stores.append(store)
        trusts.append(_trust_value(entry.get("trust")))
        counts[store] = counts.get(store, 0) + 1
    matches = scan_der_certificates("windows", ders)
    for match in matches:
        match.source = f"windows:{stores[match.index]}"
        # True means trusted for all purposes; a list holds the trusted purpose OIDs.
        match.trust = trusts[match.index]
    report.windows_store_matches = matches
    report.details["windows_store_counts"] = counts
    if errors:
        report.details["windows_store_errors"] = errors


def inspect_interpreter(python: str, label: str | None = None) -> EnvironmentReport:
    """Run the probe inside one interpreter and evaluate its trust sources."""
    report = EnvironmentReport(label or python, "inspected")
    # The probe imports packages; do not let it write __pycache__ into them.
    environment = dict(
        os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1"
    )
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as empty_directory:
            # No shell is used: the argument list is the interpreter chosen by the
            # caller with --python, "-c" and the fixed PROBE_SCRIPT, so no part of
            # it is built from untrusted text.
            completed = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
                _probe_command(python),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PROBE_TIMEOUT_SECONDS,
                cwd=empty_directory,
                env=environment,
            )
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        report.status = f"not inspected: {type(error).__name__}"
        return report
    if completed.returncode != 0:
        report.status = f"not inspected: probe exited {completed.returncode}"
        report.details = {"stderr": completed.stderr.strip()[-500:]}
        return report
    details = _parse_probe_output(completed.stdout)
    if details is None:
        report.status = "not inspected: probe output had no JSON result"
        report.details = {"stdout": completed.stdout.strip()[-500:]}
        return report
    windows_store = details.pop("windows_store", None)
    report.details = details
    packages = _dict(details.get("packages"))
    cli_paths = _dict(details.get("cli_paths"))
    report.version_checks = {
        "pip": check_version("pip", packages.get("pip")),
        "certifi": check_version("certifi", packages.get("certifi")),
    }
    for tool in ("uv", "sigstore"):
        verdict = check_version(tool, packages.get(tool))
        if verdict == "not installed" and cli_paths.get(tool):
            verdict = f"unknown (command on PATH at {cli_paths[tool]}; not executed)"
        report.version_checks[tool] = verdict
    where = details.get("certifi_where")
    if isinstance(where, str) and where:
        report.certifi_bundle = inspect_bundle_file(Path(where))
    vendored = _dict(details.get("pip_vendored_certifi"))
    if isinstance(vendored.get("where"), str) and vendored["where"]:
        report.pip_bundle = inspect_bundle_file(Path(vendored["where"]))
    details["pip_trust_source"] = pip_trust_source(details)
    if windows_store is not None:
        _windows_store_matches(report, windows_store)
    return report


_TOOLING_PATTERN = re.compile(
    r"\b(pip|certifi|uv|sigstore|setup-python|pyinstaller|truststore)\b", re.IGNORECASE
)
# user:password@ in URLs; the password may itself contain "@", so mask up to the last "@" before the host.
_CREDENTIAL_IN_URL = re.compile(r"(://)[^\s/]*@")
# KEY=value / KEY: value where the key ends with a secret-like word (GITHUB_TOKEN, TWINE_PASSWORD, client_secret).
_SECRET_ASSIGNMENT = re.compile(
    r"""(?i)(?<![A-Za-z0-9])([\w.-]*(?:token|password|passwd|secret|api[_-]?key))(\s*[=:]\s*)("[^"]*"|'[^']*'|\S+)"""
)
_SECRET_OPTION = re.compile(
    r"(?i)(--(?:password|token|api-key|secret)[=\s]+)(\"[^\"]*\"|'[^']*'|\S+)"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")


def _redact(text: str, limit: int = MAX_LINE_LENGTH) -> str:
    """Mask credentials and shorten the text before it is written to a report."""
    text = _CREDENTIAL_IN_URL.sub(r"\1***@", text)
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    text = _SECRET_OPTION.sub(lambda m: f"{m.group(1)}***", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} ***", text)
    return text if len(text) <= limit else text[:limit] + "..."


REPOSITORY_SCAN_GLOBS = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".github/actions/**/*.yml",
    ".github/actions/**/*.yaml",
    "*.bat",
    "*.cmd",
    "*.sh",
    "*.ps1",
    "tools/**/*.ps1",
    "*.spec",
    "pyproject.toml",
    "setup.cfg",
    "requirements*.txt",
    "requirements/**/*.txt",
    "constraints*.txt",
    "uv.lock",
    "Dockerfile*",
    "Makefile",
)


def scan_repository(root: Path) -> tuple[list[dict[str, object]], list[str]]:
    """List repository lines that install, pin or bundle Python tooling (read-only).

    Returns the findings and the files that could not be read. Credentials are
    masked and long lines are shortened.
    """
    paths = sorted(
        {
            path
            for pattern in REPOSITORY_SCAN_GLOBS
            for path in root.glob(pattern)
            if path.is_file()
        }
    )
    findings = []
    unreadable = []
    for path in paths:
        relative = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            unreadable.append(relative)
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if _TOOLING_PATTERN.search(line):
                findings.append(
                    {"path": relative, "line": number, "text": _redact(line.strip())}
                )
    return findings, unreadable


def _md(value: object) -> str:
    """Render a value on one Markdown line without breaking list or code formatting."""
    return str(value).replace("\r", " ").replace("\n", " ").replace("`", "'")


def _match_line(match: TargetMatch) -> str:
    """Show the commonName together with the matched names, since a match may come from another field."""
    common_name = " / ".join(match.common_names) or "-"
    trust = "" if match.trust is None else f"; trust: {match.trust}"
    return _md(
        f"  - {match.source} {match.sha256} (CN: {common_name}; matched: {', '.join(match.names)}{trust}; "
        f"{match.incident})"
    )


def _bundle_lines(title: str, bundle: BundleResult | None) -> list[str]:
    if bundle is None:
        return [f"- {title}: none"]
    if bundle.matches is None:
        return [_md(f"- {title}: {bundle.path} — {bundle.status}")]
    return [
        _md(
            f"- {title}: {bundle.path}, {bundle.certificate_count} certificates, sha256 {bundle.sha256}, "
            f"eMudhra/emSign: {len(bundle.matches)}, incident: {incident_count(bundle.matches)}"
        ),
        *(_match_line(match) for match in bundle.matches),
    ]


def _markdown(
    reports: list[EnvironmentReport],
    repository: list[dict[str, object]],
    unreadable: list[str],
) -> str:
    lines = ["# Python trust inventory", ""]
    for report in reports:
        lines.append(f"## {_md(report.label)}")
        lines.append(f"- status: {_md(report.status)}")
        if report.status != "inspected":
            for key in ("stderr", "stdout"):
                if report.details.get(key):
                    lines.append(
                        f"- {key}: {_md(_redact(str(report.details[key]), 500))}"
                    )
        for key in (
            "executable",
            "python_version",
            "is_venv",
            "openssl_version",
            "certifi_where",
            "module_files",
        ):
            if key in report.details:
                lines.append(f"- {key}: {_md(report.details[key])}")
        environment = {
            k: _redact(str(v))
            for k, v in _dict(report.details.get("environment")).items()
            if v
        }
        if "environment" in report.details:
            lines.append(
                f"- certificate environment variables: {_md(environment) if environment else 'none set'}"
            )
        for name, verdict in report.version_checks.items():
            lines.append(f"- {name}: {_md(verdict)}")
        if "bundle_path" in report.details:
            lines.extend(_bundle_lines("CA bundle", report.certifi_bundle))
        elif report.status == "inspected":
            lines.extend(_bundle_lines("certifi CA bundle", report.certifi_bundle))
            lines.append(
                f"- pip vendored certifi: {_md(report.details.get('pip_vendored_certifi'))}"
            )
            lines.append(
                f"- pip vendored truststore: {_md(report.details.get('pip_vendored_truststore'))}"
            )
            lines.extend(_bundle_lines("pip vendored CA bundle", report.pip_bundle))
            pip_config = _dict(report.details.get("pip_config"))
            lines.append(
                f"- pip config: {_md(_redact(str(pip_config), 500)) if pip_config else 'not recorded'}"
            )
            lines.append(
                f"- pip trust source: {_md(report.details.get('pip_trust_source'))}"
            )
            if report.windows_store_matches is None:
                lines.append(
                    "- Windows certificate stores: not applicable (not Windows)"
                )
            else:
                lines.append(
                    f"- Windows certificate stores {_md(report.details.get('windows_store_counts'))}: "
                    f"eMudhra/emSign: {len(report.windows_store_matches)}, "
                    f"incident: {incident_count(report.windows_store_matches)}"
                )
                lines.extend(
                    _match_line(match) for match in report.windows_store_matches
                )
                if report.details.get("windows_store_errors"):
                    lines.append(
                        f"- Windows store errors: {_md(report.details['windows_store_errors'])}"
                    )
        lines.append("")
    lines.append("## Repository references to Python tooling")
    lines.extend(
        f"- {_md(f['path'])}:{f['line']}: `{_md(f['text'])}`" for f in repository
    )
    if not repository:
        lines.append("- (none)")
    if unreadable:
        lines.append(f"- unreadable files: {_md(', '.join(unreadable))}")
    return "\n".join(lines) + "\n"


def _redacted_environment(report: dict[str, object]) -> dict[str, object]:
    """Mask credentials in the free-text fields of one report before it is written as JSON."""
    details = dict(_dict(report.get("details")))
    for key in ("stderr", "stdout"):
        if isinstance(details.get(key), str):
            details[key] = _redact(details[key], 500)
    if isinstance(details.get("environment"), dict):
        details["environment"] = {
            name: None if value is None else _redact(str(value))
            for name, value in details["environment"].items()
        }
    pip_config = details.get("pip_config")
    if isinstance(pip_config, dict) and isinstance(pip_config.get("settings"), dict):
        details["pip_config"] = {
            **pip_config,
            "settings": {
                key: _redact(str(value))
                for key, value in pip_config["settings"].items()
            },
        }
    return {**report, "details": details}


def _output_conflict(
    json_path: Path | None, markdown_path: Path | None, bundles: list[Path]
) -> str | None:
    """Refuse report paths that would overwrite each other or an inspected CA bundle."""
    outputs = [
        path.resolve() for path in (json_path, markdown_path) if path is not None
    ]
    if len(outputs) == 2 and outputs[0] == outputs[1]:
        return "--json and --markdown point to the same file"
    inspected = {bundle.resolve() for bundle in bundles}
    for output in outputs:
        if output in inspected:
            return f"report path {output} is an inspected --bundle file"
    return None


EXIT_CODES_HELP = """exit codes:
  0  every requested interpreter and bundle was inspected; certificates naming
     eMudhra/emSign do not change the exit code (this is an inventory, not a gate)
  1  at least one interpreter or bundle could not be inspected (see "status")
  2  a report file could not be written, or the report paths conflict
"""


def main(argv: list[str] | None = None) -> int:
    """Run the inventory and return the exit code described in EXIT_CODES_HELP."""
    parser = argparse.ArgumentParser(
        description="Read-only Python tooling and CA trust inventory (Issue #513).",
        epilog=EXIT_CODES_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--python",
        action="append",
        default=[],
        help="interpreter to inspect (repeatable)",
    )
    parser.add_argument(
        "--bundle",
        action="append",
        default=[],
        type=Path,
        help="CA bundle file to inspect",
    )
    parser.add_argument("--json", type=Path, help="write the full report as JSON")
    parser.add_argument(
        "--markdown",
        type=Path,
        help="write a Markdown summary (default: standard output)",
    )
    args = parser.parse_args(argv)
    interpreters = args.python or ([] if args.bundle else [sys.executable])
    reports = [inspect_interpreter(python) for python in interpreters]
    reports.extend(inspect_bundle(path) for path in args.bundle)
    repository, unreadable = scan_repository(REPO_ROOT)
    payload = {
        "environments": [_redacted_environment(r.to_dict()) for r in reports],
        "repository": repository,
        "repository_unreadable": unreadable,
        "repository_scan_globs": list(REPOSITORY_SCAN_GLOBS),
    }
    text = _markdown(reports, repository, unreadable)
    conflict = _output_conflict(args.json, args.markdown, args.bundle)
    if conflict:
        print(f"TRUST INVENTORY ERROR: {conflict}", file=sys.stderr)
        return 2
    try:
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(
                json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
                encoding="utf-8",
            )
        if args.markdown:
            args.markdown.parent.mkdir(parents=True, exist_ok=True)
            args.markdown.write_text(text, encoding="utf-8", errors="replace")
    except (OSError, ValueError, TypeError) as error:
        print(f"TRUST INVENTORY ERROR: cannot write report: {error}", file=sys.stderr)
        return 2
    if not args.markdown:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.flush()
    failed = [r for r in reports if r.status != "inspected"]
    for report in failed:
        print(
            f"TRUST INVENTORY: not inspected: {report.label}: {report.status}",
            file=sys.stderr,
        )
    if unreadable:
        print(
            f"TRUST INVENTORY WARNING: unreadable repository files: {', '.join(unreadable)}",
            file=sys.stderr,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
