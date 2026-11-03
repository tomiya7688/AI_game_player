"""配布bundleのKadoka.exeが同梱物だけで起動部品を読み込めるかを自己検査する（Issue #149）。

検査するのは「この実行中のprocessが、bundle内のPython・Native DLL・資源を使っているか」だけです。
ゲーム入力・画面取得・推論は実行しません。OSの種類やVC++再頒布パッケージの有無は判定しません。
`trust`項目は凍結した`_ssl`の信頼源を記録するだけで、インシデントとの照合は
`tools/windows_bundle_check.py launch`が行います（Issue #515）。
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

SCHEMA = "kadoka-bundle-self-check/v1"
NATIVE_LIBRARY_NAME = "kadoka_native_runtime.dll"
# 凍結appに入っていればCA bundleや信頼ストアの使い方を変えるmodule（Issue #515）。
TRUST_MODULES = ("certifi", "pip", "truststore")
# load_default_certsがWindowsで読む証明書ストア（CPythonのssl._load_windows_store_certs）。
WINDOWS_TRUST_STORES = ("CA", "ROOT")
VERIFY_PATH_FIELDS = (
    "cafile",
    "capath",
    "openssl_cafile_env",
    "openssl_cafile",
    "openssl_capath_env",
    "openssl_capath",
)


# {
#   責務: [BundleCheckResult: 自己検査1項目の合否と、失敗時に原因を特定できる説明を保持する]
#   フィールド: [name: 検査項目の固定名, ok: 合格ならTrue, detail: 確認した値または失敗理由]
# }
@dataclass(frozen=True)
class BundleCheckResult:
    name: str
    ok: bool
    detail: str


# {
#   責務: [_is_inside: pathがbundle_root配下かを、存在しないpathでも例外なしで判定する]
#   処理: [両方をresolveして比較し、解決できないpathは配下ではないと扱う]
#   引数: [path: 調べるpath, bundle_root: Kadoka.exeのあるdirectory]
#   戻り値: [bool: bundle_root自身または配下ならTrue]
# }
def _is_inside(path: Path, bundle_root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(bundle_root)
    except (OSError, RuntimeError, ValueError):
        return False


# {
#   責務: [_check_frozen: PyInstallerで凍結されたprocessとして起動しているかを確認する]
#   処理: [sys.frozenとsys._MEIPASSの両方を要求する。開発用Pythonからの実行は不合格]
#   引数: [なし] 戻り値: [BundleCheckResult: 凍結状態の検査結果]
# }
def _check_frozen() -> BundleCheckResult:
    frozen = bool(getattr(sys, "frozen", False))
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if frozen and bundle_dir:
        return BundleCheckResult("frozen", True, f"_MEIPASS={bundle_dir}")
    return BundleCheckResult("frozen", False, "not running from a PyInstaller bundle")


# {
#   責務: [_check_import_paths: Python moduleの探索先とapp packageがbundle内だけにあることを確認する]
#   処理: [sys.pathの全要素（空文字は作業directory）とai_game_player.__file__をbundle_rootと比較する]
#   引数: [bundle_root: Kadoka.exeのあるdirectory]
#   戻り値: [BundleCheckResult: bundle外のpathがあれば列挙した不合格結果]
# }
def _check_import_paths(bundle_root: Path) -> BundleCheckResult:
    import ai_game_player

    # Python treats an empty sys.path entry as the current working directory.
    outside = [
        entry or f"''={Path.cwd()}"
        for entry in sys.path
        if not _is_inside(Path(entry) if entry else Path.cwd(), bundle_root)
    ]
    package_file = Path(ai_game_player.__file__ or "")
    if not _is_inside(package_file, bundle_root):
        outside.append(f"ai_game_player={package_file}")
    if outside:
        return BundleCheckResult("import_paths", False, f"outside bundle: {outside}")
    return BundleCheckResult(
        "import_paths", True, f"{len(sys.path)} sys.path entries inside bundle"
    )


# {
#   責務: [_check_native_runtime: 通常の探索手順でbundle直下のNative DLLがABI互換で読めることを確認する]
#   処理: [discover_native_runtimeを環境変数指定なしの前提で呼び、AVAILABLEかつDLLがexe隣接であることを要求して閉じる]
#   引数: [bundle_root: Kadoka.exeのあるdirectory]
#   戻り値: [BundleCheckResult: 読込状態とDLLのpath、または失敗理由]
# }
def _check_native_runtime(bundle_root: Path) -> BundleCheckResult:
    from ai_game_player.runtime.native import NativeLoadStatus, discover_native_runtime

    result = discover_native_runtime()
    try:
        if (
            result.status is not NativeLoadStatus.AVAILABLE
            or result.library_path is None
        ):
            return BundleCheckResult(
                "native_runtime", False, f"{result.status.value}: {result.reason}"
            )
        if result.library_path.resolve().parent != bundle_root:
            return BundleCheckResult(
                "native_runtime",
                False,
                f"loaded outside bundle root: {result.library_path}",
            )
        return BundleCheckResult("native_runtime", True, str(result.library_path))
    finally:
        if result.runtime is not None:
            result.runtime.close()


# {
#   責務: [_check_native_safety: 安全検証の補助経路が同じbundle直下のDLLを読めることを確認する]
#   処理: [try_loadの結果がNoneでなく、読んだDLLの絶対pathの親がbundle_rootであることを確認する。
#         ctypesはDLLを解放しないため、検査後のprocess終了で解放される]
#   引数: [bundle_root: Kadoka.exeのあるdirectory]
#   戻り値: [BundleCheckResult: 安全検証DLLの読込結果]
# }
def _check_native_safety(bundle_root: Path) -> BundleCheckResult:
    from ai_game_player.safety_guard import NativeSafetyValidator

    validator = NativeSafetyValidator.try_load()
    if validator is None:
        return BundleCheckResult(
            "native_safety", False, "NativeSafetyValidator.try_load returned None"
        )
    loaded = Path(getattr(validator.library, "_name", "") or "")
    if (
        not loaded.is_absolute()
        or loaded.name.lower() != NATIVE_LIBRARY_NAME
        or loaded.resolve().parent != bundle_root
    ):
        return BundleCheckResult(
            "native_safety", False, f"loaded outside bundle root: {loaded}"
        )
    return BundleCheckResult("native_safety", True, str(loaded))


# {
#   責務: [_check_assets: GUIが参照する同梱資源directoryがbundle内に存在し空でないことを確認する]
#   処理: [ai_game_player package直下のassetsを数え、bundle外や空なら不合格にする]
#   引数: [bundle_root: Kadoka.exeのあるdirectory]
#   戻り値: [BundleCheckResult: 資源file数、または失敗理由]
# }
def _check_assets(bundle_root: Path) -> BundleCheckResult:
    import ai_game_player

    assets = Path(ai_game_player.__file__ or "").parent / "assets"
    if not assets.is_dir() or not _is_inside(assets, bundle_root):
        return BundleCheckResult("assets", False, f"missing: {assets}")
    count = sum(1 for path in assets.rglob("*") if path.is_file())
    if count == 0:
        return BundleCheckResult("assets", False, f"empty: {assets}")
    return BundleCheckResult("assets", True, f"{count} files in {assets}")


# {
#   責務: [_extension_outside: 拡張moduleのfileがbundle外にあれば、そのpathを返す]
#   処理: [importしたmoduleの__file__をbundle_rootと比較する。__file__が無い組込みmoduleはbundle外扱い]
#   引数: [module: importしたmodule, bundle_root: Kadoka.exeのあるdirectory]
#   戻り値: [str | None: bundle外ならpathの説明、bundle内ならNone]
# }
def _extension_outside(module: object, bundle_root: Path) -> str | None:
    location = getattr(module, "__file__", None)
    if location and _is_inside(Path(location), bundle_root):
        return None
    return f"{getattr(module, '__name__', module)} loaded from {location!r}"


# {
#   責務: [_check_tk: bundle内の_tkinterでTcl/Tkを読み込み、Tk rootを作成・破棄できることを確認する]
#   処理: [_tkinterのfileがbundle内かを見てから、rootを表示せずに作成し、idle処理後に必ず破棄する]
#   引数: [bundle_root: Kadoka.exeのあるdirectory]
#   戻り値: [BundleCheckResult: Tkのversion、またはbundle外の読込先]
# }
def _check_tk(bundle_root: Path) -> BundleCheckResult:
    import _tkinter
    import tkinter as tk

    if outside := _extension_outside(_tkinter, bundle_root):
        return BundleCheckResult("tk", False, outside)
    root = tk.Tk()
    try:
        root.withdraw()
        root.update_idletasks()
        return BundleCheckResult("tk", True, f"Tk {root.call('info', 'patchlevel')}")
    finally:
        root.destroy()


# {
#   責務: [_check_ssl: 同梱OpenSSLを読み込み既定のTLS contextを作れることを確認する]
#   処理: [_sslのfileがbundle内かを見てからssl.create_default_contextを呼ぶ。通信はしない。CAの内容は_check_trustで記録する]
#   引数: [bundle_root: Kadoka.exeのあるdirectory]
#   戻り値: [BundleCheckResult: OpenSSL version、またはbundle外の読込先]
# }
def _check_ssl(bundle_root: Path) -> BundleCheckResult:
    import _ssl
    import ssl

    if outside := _extension_outside(_ssl, bundle_root):
        return BundleCheckResult("ssl", False, outside)
    ssl.create_default_context()
    return BundleCheckResult("ssl", True, ssl.OPENSSL_VERSION)


# {
#   責務: [_check_trust: 凍結した_sslの既定TLS contextが実際に信頼する証明書と、その出所を記録する]
#   処理: [OpenSSLの既定path、SSL_CERT_*、TRUST_MODULESの有無、既定contextのCA一覧（DERのbase64）、
#         WindowsではCA・ROOTストアのx509_asn証明書をsinkへ書く。get_ca_certsはCA以外の証明書を返さないため、
#         ストアは別に記録する。certifiが入っていれば、where()がbundle内であることを要求する。
#         incidentとの照合はtrust_inventoryを持つtools/windows_bundle_check.pyが行う]
#   引数: [bundle_root: Kadoka.exeのあるdirectory, sink: reportの"trust"へ書く辞書]
#   戻り値: [BundleCheckResult: CA件数と出所、またはbundle外のcertifi]
# }
def _check_trust(bundle_root: Path, sink: dict[str, object]) -> BundleCheckResult:
    import base64
    import importlib
    import importlib.util
    import os
    import ssl

    paths = ssl.get_default_verify_paths()
    context = ssl.create_default_context()
    certificates = context.get_ca_certs(binary_form=True)
    modules: dict[str, str | None] = {}
    for name in TRUST_MODULES:
        spec = importlib.util.find_spec(name)
        modules[name] = None if spec is None else str(spec.origin)
    certifi_where = None
    if modules["certifi"] is not None:
        # A static `import certifi` would make PyInstaller collect certifi and its
        # cacert.pem, changing what the bundle carries; import it only if present.
        certifi_where = importlib.import_module("certifi").where()
    store_certificates = [
        {
            "store": store,
            "trust": trust if isinstance(trust, bool) else sorted(trust),
            "der": base64.b64encode(der).decode("ascii"),
        }
        for store in (WINDOWS_TRUST_STORES if sys.platform == "win32" else ())
        for der, encoding, trust in ssl.enum_certificates(store)  # type: ignore[attr-defined]
        if encoding == "x509_asn"
    ]
    sink.update(
        {
            "platform": sys.platform,
            "default_verify_paths": {
                field: getattr(paths, field) for field in VERIFY_PATH_FIELDS
            },
            "environment": {
                name: os.environ.get(name) for name in ("SSL_CERT_FILE", "SSL_CERT_DIR")
            },
            "modules": modules,
            "certifi_where": certifi_where,
            "cert_store_stats": context.cert_store_stats(),
            "context_ca_certs": [
                base64.b64encode(der).decode("ascii") for der in certificates
            ],
            "windows_store_certs": store_certificates,
        }
    )
    if certifi_where is not None and not _is_inside(Path(certifi_where), bundle_root):
        return BundleCheckResult(
            "trust", False, f"certifi bundle outside bundle: {certifi_where}"
        )
    present = sorted(name for name, origin in modules.items() if origin)
    return BundleCheckResult(
        "trust",
        True,
        f"{len(certificates)} CA certificates in the default context; "
        f"{len(store_certificates)} Windows CA/ROOT store certificates; "
        f"cafile={paths.cafile!r} capath={paths.capath!r}; trust modules={present}",
    )


# {
#   責務: [_run_guarded: 1項目の検査を実行し、例外を不合格結果へ変換して他項目の検査を続ける]
#   処理: [検査関数を呼び、Exceptionは型と内容をdetailへ残す]
#   引数: [name: 例外時に使う項目名, check: 検査関数]
#   戻り値: [BundleCheckResult: 検査関数の結果、または例外による不合格]
# }
def _run_guarded(
    name: str, check: Callable[[], BundleCheckResult]
) -> BundleCheckResult:
    try:
        return check()
    except Exception as error:  # noqa: BLE001 - 自己検査は全例外を項目の失敗として報告する
        return BundleCheckResult(name, False, f"{type(error).__name__}: {error}")


# {
#   責務: [collect_bundle_checks: 配布bundleの起動部品を全項目検査し、結果一覧を返す]
#   処理: [凍結状態・import path・Native DLL・安全検証DLL・資源・Tk・SSL・信頼源を順に独立して検査する]
#   引数: [bundle_root: Kadoka.exeのあるdirectory。既定は実行中のsys.executableの親,
#         trust_sink: 信頼源の詳細を受け取る辞書。Noneなら捨てる]
#   戻り値: [list[BundleCheckResult]: 項目順の検査結果。1件でもokがFalseなら不合格]
# }
def collect_bundle_checks(
    bundle_root: Path | None = None, trust_sink: dict[str, object] | None = None
) -> list[BundleCheckResult]:
    root = (bundle_root or Path(sys.executable).parent).resolve()
    sink: dict[str, object] = {} if trust_sink is None else trust_sink
    checks: list[tuple[str, Callable[[], BundleCheckResult]]] = [
        ("frozen", _check_frozen),
        ("import_paths", lambda: _check_import_paths(root)),
        ("native_runtime", lambda: _check_native_runtime(root)),
        ("native_safety", lambda: _check_native_safety(root)),
        ("assets", lambda: _check_assets(root)),
        ("tk", lambda: _check_tk(root)),
        ("ssl", lambda: _check_ssl(root)),
        ("trust", lambda: _check_trust(root, sink)),
    ]
    return [_run_guarded(name, check) for name, check in checks]


# {
#   責務: [run_bundle_self_check: 自己検査を実行し、JSON reportを書いて終了codeを返す]
#   処理: [GUI版exeは標準出力を持たないため、指定pathへUTF-8 JSONを書く。親directoryは作成しない。
#         信頼源の詳細は"trust"へ書く]
#   引数: [report_path: 書き込み先のJSON file]
#   戻り値: [int: 全項目合格なら0、1項目でも不合格なら1]
#   エラー: [OSError: report_pathへ書けない場合]
# }
def run_bundle_self_check(report_path: Path) -> int:
    trust: dict[str, object] = {}
    results = collect_bundle_checks(trust_sink=trust)
    ok = all(result.ok for result in results)
    report = {
        "schema": SCHEMA,
        "ok": ok,
        "executable": str(Path(sys.executable).resolve()),
        "checks": [asdict(result) for result in results],
        "trust": trust,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if ok else 1
