"""長時間Sessionの資源安定性を判定する純粋core(Issue #787)。

時計・OS・fileに触れない。同じsamplesなら同じreportを返す。
設計書: doc/design/issue-787-long-session-resource-stability-gate.md
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Callable, Mapping, Sequence
from typing import Any

SAMPLE_SCHEMA = "resource-stability-sample/v1"
REPORT_SCHEMA = "resource-stability-report/v1"
PROFILE_SCHEMA = "resource-stability-profile/v1"

MIB = 1024 * 1024
LAYERS = ("fast", "integration", "real_hardware")
# 層ごとに許可する実行profile(それ以外はprofile_invalid)
LAYER_RUN_PROFILES = {
    "fast": ("fast", "deep"),
    "integration": ("pr", "deep"),
    "real_hardware": ("deep",),
}
SOURCES = ("process", "external", "registry_snapshot", "synthetic")
UNSUPPORTED_REASONS = (
    "platform",
    "no_counter",
    "tool_missing",
    "access_denied",
    "probe_error",
    "registry_not_connected",
    "snapshot_version",
    "scan_truncated",
)
PHASES = ("warmup", "steady", "closing", "post_close", "pre_open")
DEFAULT_MAX_SAMPLES = 20000
NONE_RATIO_LIMIT = 0.05
MIN_TAIL_SAMPLES = 16
MIN_TAIL_BINS = 8
MAX_TAIL_BINS = 100

# 固定のmetric語彙: id -> (単位, class)
STATIC_METRICS: dict[str, tuple[str, str]] = {
    "mem.resident_bytes": ("bytes", "bytes"),
    "mem.private_bytes": ("bytes", "bytes"),
    "mem.os_peak_bytes": ("bytes", "bytes"),
    "py.heap_traced_bytes": ("bytes", "bytes"),
    "native.alloc_count": ("count", "count"),
    "native.copy_count": ("count", "count"),
    "gpu.process_vram_bytes": ("bytes", "bytes"),
    "gpu.device_vram_bytes": ("bytes", "bytes"),
    "os.handle_count": ("count", "count"),
    "os.gdi_objects": ("count", "count"),
    "os.user_objects": ("count", "count"),
    "registry.active_leases": ("count", "count"),
    "registry.releasing_leases": ("count", "count"),
    "registry.failed_leases": ("count", "count"),
    "registry.max_cleanup_latency_ms": ("ms", "latency"),
    "temp.file_count": ("count", "count"),
    "temp.bytes": ("bytes", "bytes"),
    "task.pending_count": ("count", "count"),
}
_KIND_METRIC = re.compile(r"^registry\.kind\.[a-z0-9_-]+\.(count|bytes)$")
_QUEUE_METRIC = re.compile(r"^queue\.length\.[a-z0-9_-]+$")
# hard limit(コード側。profileが超えるとprofile_invalid)
_BYTES_REL_LIMIT = 0.05
_BYTES_ABS_LIMIT = 64 * MIB
_ABS_LIMITS = {
    "os.handle_count": 64,
    "os.gdi_objects": 8,
    "os.user_objects": 8,
    "temp.file_count": 16,
}
_TREE_PREFIXES = ("mem.", "gpu.", "os.", "py.", "native.")
_FIRST_SESSION_EXEMPT_PREFIXES = ("mem.", "gpu.", "os.")


# {
#   責務: [ProfileError: profileまたは入力samplesの不正(CLIの終了コード2・profile_invalid)]
#   フィールド: [message: 不正の内容]
# }
class ProfileError(ValueError):
    pass


# {
#   責務: [metric_traits: metric IDの単位とclassを返す(語彙外はNone)]
#   処理: [固定語彙を引く, registry.kind.* と queue.length.* の動的IDを判定する]
#   引数: [metric_id: 対象ID]
#   戻り値: [(unit, class)またはNone]
# }
def metric_traits(metric_id: str) -> tuple[str, str] | None:
    if metric_id in STATIC_METRICS:
        return STATIC_METRICS[metric_id]
    if _KIND_METRIC.match(metric_id):
        return (
            ("bytes", "bytes") if metric_id.endswith(".bytes") else ("count", "count")
        )
    if _QUEUE_METRIC.match(metric_id):
        return ("count", "count")
    return None


# {
#   責務: [nearest_rank: nearest-rank方式の百分位(#811と同じ)]
#   処理: [昇順に並べる, ceil(q*n)番目を返す]
#   引数: [values: 値の列, q: 0..1の分位]
#   戻り値: [百分位の値(空ならNaN)]
# }
def nearest_rank(values: Sequence[float], q: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


# {
#   責務: [theil_sen_slope: Theil-Sen傾き(全対傾きの中央値)]
#   処理: [x増加の組だけ傾きを集める, 中央値を返す]
#   引数: [xs: x座標, ys: y座標]
#   戻り値: [傾き(計算不能ならNaN)]
# }
def theil_sen_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    slopes = [
        (ys[j] - ys[i]) / (xs[j] - xs[i])
        for i in range(len(xs))
        for j in range(i + 1, len(xs))
        if xs[j] > xs[i]
    ]
    return statistics.median(slopes) if slopes else math.nan


def _num(value: Any) -> bool:
    # 有限の数だけを数とみなす(NaN・無限大は上限や許容の検査をすり抜けるため拒否)
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _fail(message: str) -> ProfileError:
    return ProfileError(message)


# {
#   責務: [validate_profile: profileを検証しhard limit超過を拒否する(fail-closed)]
#   処理: [schemaと層・実行profileの組合せを確認, metric語彙と許容値をhard limitと比較, enforcementの組合せを確認]
#   引数: [raw: 読み込んだprofile辞書]
#   戻り値: [検証済みprofile(入力と同一object)]
#   エラー: [ProfileError: 不正なprofile]
# }
def validate_profile(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema") != PROFILE_SCHEMA:
        raise _fail("profile schema mismatch")
    layers = raw.get("layers")
    if not isinstance(layers, dict) or not layers:
        raise _fail("profile layers missing")
    for layer, body in layers.items():
        if layer not in LAYERS or not isinstance(body, dict):
            raise _fail(f"unknown layer: {layer}")
        _validate_layer(layer, body)
    enforcement = raw.get("enforcement")
    if not isinstance(enforcement, dict):
        raise _fail("enforcement missing")
    for layer in layers:
        mode = enforcement.get(layer)
        allowed = ("enforce", "observe") if layer == "integration" else ("enforce",)
        if mode not in allowed:
            raise _fail(f"enforcement.{layer} must be one of {allowed}")
    return raw


def _validate_layer(layer: str, body: dict[str, Any]) -> None:
    profiles = body.get("run_profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise _fail(f"{layer}.run_profiles missing")
    for name, cfg in profiles.items():
        if name not in LAYER_RUN_PROFILES[layer]:
            raise _fail(f"layer {layer} does not allow run profile {name}")
        _validate_run_profile(layer, name, cfg)
    metrics = body.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        raise _fail(f"{layer}.metrics missing")
    for metric_id, spec in metrics.items():
        _validate_metric(layer, metric_id, spec)
    must = body.get("must_judge")
    if not isinstance(must, dict):
        raise _fail(f"{layer}.must_judge missing")
    if not any(isinstance(ids, list) and ids for ids in must.values()):
        raise _fail(f"{layer}.must_judge must list at least one metric")
    for group, ids in must.items():
        if group not in ("any", "windows", "linux") or not isinstance(ids, list):
            raise _fail(f"{layer}.must_judge group invalid: {group}")
        for metric_id in ids:
            if metric_id not in metrics:
                raise _fail(f"{layer}.must_judge references unknown metric {metric_id}")


def _validate_run_profile(layer: str, name: str, cfg: Any) -> None:
    where = f"{layer}.{name}"
    if not isinstance(cfg, dict):
        raise _fail(f"{where} must be an object")
    segments = cfg.get("segments")
    if not isinstance(segments, int) or not 4 <= segments <= 8:
        raise _fail(f"{where}.segments must be 4..8")
    per_segment = cfg.get("min_samples_per_segment")
    if not isinstance(per_segment, int) or per_segment < 8:
        raise _fail(f"{where}.min_samples_per_segment must be >= 8")
    limit_samples = cfg.get("max_samples", DEFAULT_MAX_SAMPLES)
    if not _is_int(limit_samples) or not 0 < limit_samples <= DEFAULT_MAX_SAMPLES:
        raise _fail(f"{where}.max_samples must be an int in 1..{DEFAULT_MAX_SAMPLES}")
    sessions = cfg.get("sessions")
    minimum = 1 if layer == "integration" else 4
    if not isinstance(sessions, int) or sessions < minimum:
        raise _fail(f"{where}.sessions must be >= {minimum}")
    for key in ("warmup_steps", "warmup_seconds", "settle_ms"):
        if not _num(cfg.get(key)) or cfg[key] < 0:
            raise _fail(f"{where}.{key} must be a non-negative number")
    if layer == "integration":
        interval = cfg.get("interval_seconds")
        if not _num(interval) or not 0.25 <= interval <= 10:
            raise _fail(f"{where}.interval_seconds must be 0.25..10")
    else:
        for key in ("steps_per_session", "sample_every_steps"):
            if not isinstance(cfg.get(key), int) or cfg[key] < 1:
                raise _fail(f"{where}.{key} must be a positive int")
        expected = sessions * (cfg["steps_per_session"] // cfg["sample_every_steps"])
        if expected > limit_samples:
            raise _fail(f"{where} would exceed max_samples")


def _check_tolerance(
    where: str, metric_id: str, field: dict[str, Any], cls: str
) -> None:
    if not isinstance(field, dict) or not _num(field.get("abs")):
        raise _fail(f"{where} needs numeric abs")
    absolute = field["abs"]
    rel = field.get("rel", 0)
    if not _num(rel) or rel < 0 or absolute < 0:
        raise _fail(f"{where} must be non-negative")
    if metric_id.startswith("registry.") and not metric_id.endswith(".bytes"):
        # Registry系のcount・件数は許容0固定(kindのbytesだけbytes用hard limit)
        if absolute != 0 or rel != 0:
            raise _fail(f"{where} must be 0 for registry metrics")
        return
    if cls == "count" and rel != 0:
        # 件数の許容は絶対値だけ(baselineが大きいと相対許容が実質的な緩和になるため)
        raise _fail(f"{where}.rel must be 0 for count metrics")
    if cls == "bytes" and (rel > _BYTES_REL_LIMIT or absolute > _BYTES_ABS_LIMIT):
        raise _fail(f"{where} exceeds the bytes hard limit")
    limit = _ABS_LIMITS.get(metric_id)
    if limit is not None and absolute > limit:
        raise _fail(f"{where} exceeds the hard limit {limit}")


def _validate_metric(layer: str, metric_id: str, spec: Any) -> None:
    where = f"{layer}.metrics.{metric_id}"
    traits = metric_traits(metric_id)
    if traits is None:
        raise _fail(f"{where}: metric id outside the vocabulary")
    if not isinstance(spec, dict):
        raise _fail(f"{where} must be an object")
    required_on = spec.get("required_on")
    if not isinstance(required_on, list) or any(
        o not in ("windows", "linux", "any") for o in required_on
    ):
        raise _fail(f"{where}.required_on invalid")
    cls = spec.get("class")
    if cls != traits[1]:
        raise _fail(f"{where}.class must be {traits[1]}")
    judge = spec.get("judge", True)
    if judge not in (True, False, "exclusive_gpu"):
        raise _fail(f"{where}.judge invalid")
    if cls == "latency":
        ceiling = spec.get("ceiling")
        if not _num(ceiling) or not 0 < ceiling <= 2000:
            raise _fail(f"{where}.ceiling must be in (0, 2000]")
        return
    _check_tolerance(f"{where}.tolerance", metric_id, spec.get("tolerance"), cls)  # type: ignore[arg-type]
    _check_tolerance(f"{where}.recovery", metric_id, spec.get("recovery"), cls)  # type: ignore[arg-type]
    if "ceiling" in spec and not _num(spec["ceiling"]):
        raise _fail(f"{where}.ceiling must be numeric")


def _tol(field: Mapping[str, Any], reference: float) -> float:
    return max(float(field["abs"]), float(field.get("rel", 0)) * abs(reference))


def _split(values: list[Any], parts: int) -> list[list[Any]]:
    n = len(values)
    return [values[i * n // parts : (i + 1) * n // parts] for i in range(parts)]


def _med(values: Sequence[float]) -> float:
    return float(statistics.median(values))


def _host_os(host_profile: str) -> str:
    head = str(host_profile).split("-")[0].lower()
    return head if head in ("windows", "linux") else "other"


# {
#   責務: [parse_samples: samples.jsonlの行(辞書)をheader・sample・footerに分け検証する]
#   処理: [1行目がheaderか確認, schemaとphaseを検証, run_idの一致を確認]
#   引数: [rows: JSON行を読んだ辞書の列]
#   戻り値: [(header, samples, footer)]
#   エラー: [ProfileError: header欠落・未知schema・語彙外ID]
# }
def parse_samples(
    rows: Sequence[Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
    if not rows or not isinstance(rows[0], dict) or rows[0].get("kind") != "header":
        raise _fail("samples must start with a header line")
    header = rows[0]
    if header.get("schema") != SAMPLE_SCHEMA:
        raise _fail("unknown samples schema")
    for key in (
        "run_id",
        "layer",
        "run_profile",
        "host_profile",
        "scenario",
        "metrics",
        "declarations",
    ):
        if key not in header:
            raise _fail(f"header.{key} missing")
    for key in ("run_id", "layer", "run_profile", "host_profile", "scenario"):
        if not isinstance(header[key], str) or not header[key]:
            raise _fail(f"header.{key} must be a non-empty string")
    if not isinstance(header["metrics"], dict):
        raise _fail("header.metrics must be an object")
    for metric_id, info in header["metrics"].items():
        if metric_traits(metric_id) is None:
            raise _fail(f"metric id outside the vocabulary: {metric_id}")
        if not isinstance(info, dict) or info.get("source") not in SOURCES:
            raise _fail(f"header.metrics.{metric_id}.source invalid")
        if info.get("status") not in ("measured", "unsupported"):
            raise _fail(f"header.metrics.{metric_id}.status invalid")
        if (
            info.get("status") == "unsupported"
            and info.get("unsupported_reason") not in UNSUPPORTED_REASONS
        ):
            raise _fail(f"header.metrics.{metric_id}.unsupported_reason invalid")
    _validate_declarations(header["declarations"])
    samples: list[dict[str, Any]] = []
    footer: dict[str, Any] | None = None
    for row in rows[1:]:
        if not isinstance(row, dict) or row.get("schema") != SAMPLE_SCHEMA:
            raise _fail("unknown samples schema")
        if row.get("run_id") != header["run_id"]:
            raise _fail("run_id mismatch")
        if footer is not None:
            raise _fail("rows after the footer")
        if row.get("kind") == "footer":
            _validate_footer(row)
            footer = row
        elif row.get("kind") == "sample":
            _validate_sample(row)
            samples.append(row)
        else:
            raise _fail("unknown row kind")
    # 時刻順(同時刻はstep順)に整える。first_tや区間分割が入力順に依存しないようにする
    samples.sort(key=lambda r: (r["session_index"], r["t_monotonic_ns"], r["step"]))
    return header, samples, footer


def _validate_declarations(decl: Any) -> None:
    if not isinstance(decl, dict):
        raise _fail("header.declarations must be an object")
    for key in ("declared_retained", "declared_ceiling"):
        table = decl.get(key, {})
        if not isinstance(table, dict) or not all(
            metric_traits(k) is not None and _num(v) for k, v in table.items()
        ):
            raise _fail(f"header.declarations.{key} invalid")
    fill = decl.get("declared_fill_steps", 0)
    if not _is_int(fill) or fill < 0:
        raise _fail("header.declarations.declared_fill_steps invalid")
    if not isinstance(decl.get("exclusive_gpu", False), bool):
        raise _fail("header.declarations.exclusive_gpu must be a boolean")


def _validate_sample(row: dict[str, Any]) -> None:
    if row.get("phase") not in PHASES or not isinstance(row.get("values"), dict):
        raise _fail("sample phase/values invalid")
    for key in ("session_index", "step", "t_monotonic_ns"):
        if not _is_int(row.get(key)) or row[key] < 0:
            raise _fail(f"sample.{key} must be a non-negative integer")
    for metric_id, value in row["values"].items():
        if metric_traits(metric_id) is None:
            raise _fail(f"metric id outside the vocabulary: {metric_id}")
        if value is not None and not _num(value):
            raise _fail(f"sample value for {metric_id} must be a finite number or null")


def _validate_footer(row: dict[str, Any]) -> None:
    orphans = row.get("orphan_count", 0)
    if not _is_int(orphans) or orphans < 0:
        raise _fail("footer.orphan_count must be a non-negative integer")
    for key in ("total_steps",):
        if key in row and (not _is_int(row[key]) or row[key] < 0):
            raise _fail(f"footer.{key} must be a non-negative integer")
    exit_code = row.get("command_exit_code")
    if exit_code is not None and not _is_int(exit_code):
        raise _fail("footer.command_exit_code must be an integer or null")
    sessions = row.get("sessions", [])
    if not isinstance(sessions, list) or not all(
        isinstance(item, dict) and _is_int(item.get("index")) for item in sessions
    ):
        raise _fail("footer.sessions invalid")


def _reason(
    code: str,
    observed: Any,
    limit: Any,
    metric: str | None = None,
    session: int | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "observed": observed, "limit": limit}
    if metric is not None:
        item["metric"] = metric
    if session is not None:
        item["session"] = session
    return item


class _Run:
    # 1回のevaluate_runの作業状態(内部用)
    def __init__(
        self,
        profile: dict[str, Any],
        header: dict[str, Any],
        samples: list[dict[str, Any]],
        footer: dict[str, Any] | None,
    ):
        layer = header["layer"]
        run_profile = header["run_profile"]
        if (
            layer not in profile["layers"]
            or run_profile not in profile["layers"][layer]["run_profiles"]
        ):
            raise _fail("layer/run_profile not defined in the profile")
        self.profile = profile
        self.header = header
        self.layer: str = layer
        self.cfg: dict[str, Any] = profile["layers"][layer]["run_profiles"][run_profile]
        self.metrics_spec: dict[str, Any] = profile["layers"][layer]["metrics"]
        self.host_os = _host_os(header["host_profile"])
        decl = header["declarations"]
        self.retained: dict[str, float] = dict(decl.get("declared_retained", {}))
        self.ceilings: dict[str, float] = dict(decl.get("declared_ceiling", {}))
        self.fill_steps = int(decl.get("declared_fill_steps", 0))
        self.exclusive_gpu = bool(decl.get("exclusive_gpu", False))
        self.footer = footer or {}
        self.reasons: list[dict[str, Any]] = []
        self.samples = samples
        self.sessions = sorted({s["session_index"] for s in samples})
        self.stride = max([s["step"] for s in samples] + [0]) + 1
        self.boundary_step = max(int(self.cfg["warmup_steps"]), self.fill_steps)
        self.boundary_seconds = float(self.cfg["warmup_seconds"])
        first_t: dict[int, int] = {}
        for s in samples:
            first_t.setdefault(s["session_index"], s["t_monotonic_ns"])
        self.first_t = first_t

    def is_steady(self, s: dict[str, Any]) -> bool:
        if s["phase"] not in ("warmup", "steady"):
            return False
        elapsed = (s["t_monotonic_ns"] - self.first_t[s["session_index"]]) / 1e9
        return s["step"] >= self.boundary_step and elapsed >= self.boundary_seconds

    def required(self, metric_id: str) -> bool:
        spec = self.metrics_spec.get(metric_id)
        if spec is None:
            return False
        return "any" in spec["required_on"] or self.host_os in spec["required_on"]

    def judged_spec(self, metric_id: str) -> bool:
        spec = self.metrics_spec.get(metric_id)
        if spec is None:
            return False
        judge = spec.get("judge", True)
        return self.exclusive_gpu if judge == "exclusive_gpu" else bool(judge)


def _stats(
    series: list[tuple[float, float]], all_values: list[float], segments: int
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "peak": max(all_values) if all_values else None,
        "p50": None,
        "p95": None,
        "end": None,
        "end_minus_start": None,
        "tail_growth": None,
        "tail_slope_per_1000_steps": None,
        "segment_medians": [],
    }
    values = [v for _, v in series]
    if not values:
        return out
    out["p50"] = _med(values)
    out["p95"] = nearest_rank(values, 0.95)
    parts = _split(series, segments)
    medians = [_med([v for _, v in part]) if part else math.nan for part in parts]
    out["segment_medians"] = medians
    out["end"] = medians[-1]
    out["end_minus_start"] = medians[-1] - medians[0]
    out["tail_growth"] = medians[-1] - medians[math.ceil(segments / 2) - 1]
    return out


def _tail_drift(
    series: list[tuple[float, float]], segments: int
) -> tuple[float, float] | None:
    # 後半区間のTheil-Sen傾き(1step当たり)と後半の幅。計算不能ならNone
    tail = [p for part in _split(series, segments)[segments // 2 :] for p in part]
    if len(tail) < MIN_TAIL_SAMPLES:
        return None
    bins = min(MAX_TAIL_BINS, len(tail) // 2)
    if bins < MIN_TAIL_BINS:
        return None
    chunks = _split(tail, bins)
    xs = [_med([x for x, _ in c]) for c in chunks]
    ys = [_med([y for _, y in c]) for c in chunks]
    slope = theil_sen_slope(xs, ys)
    if math.isnan(slope):
        return None
    return slope, tail[-1][0] - tail[0][0]


def _evaluate_metric(run: _Run, metric_id: str, result: dict[str, Any]) -> None:
    spec = run.metrics_spec.get(metric_id)
    judged = spec is not None and run.judged_spec(metric_id)
    segments = int(run.cfg["segments"])
    steady: list[tuple[float, float]] = []
    by_session: dict[int, list[tuple[float, float]]] = {}
    steady_total = 0
    all_values: list[float] = []
    for s in run.samples:
        value = s["values"].get(metric_id)
        if value is not None:
            all_values.append(float(value))
        if run.is_steady(s):
            steady_total += 1
            if value is not None:
                point = (s["session_index"] * run.stride + s["step"], float(value))
                steady.append(point)
                by_session.setdefault(s["session_index"], []).append(point)
    result.update(_stats(steady, all_values, segments))
    none_ratio = (steady_total - len(steady)) / steady_total if steady_total else 1.0
    if none_ratio > NONE_RATIO_LIMIT:
        result["status"] = "unreliable"
        result["judged"] = False
        result["reasons"].append("metric_unreliable")
        if run.required(metric_id):
            result["verdict"] = "fail"
            run.reasons.append(
                _reason(
                    "metric_unreliable",
                    round(none_ratio, 4),
                    NONE_RATIO_LIMIT,
                    metric_id,
                )
            )
        return
    if not judged:
        return
    result["judged"] = True
    assert spec is not None
    cls = spec["class"]
    failed = False
    ceiling_candidates = [
        c for c in (run.ceilings.get(metric_id), spec.get("ceiling")) if c is not None
    ]
    ceiling = min(ceiling_candidates) if ceiling_candidates else None
    if ceiling is not None:
        result["declared_ceiling"] = ceiling
        if all_values and max(all_values) > ceiling:
            failed = True
            run.reasons.append(
                _reason("ceiling_exceeded", max(all_values), ceiling, metric_id)
            )
    if cls == "latency":
        result["tolerance"] = {"abs": 0}
    else:
        # 成長はSessionごとに判定する(Session内で増えclose時に解放される漏れを、連結で薄めない)
        failed |= _judge_growth(run, metric_id, spec, by_session, segments, result)
    result["verdict"] = "fail" if failed else "pass"


def _judge_growth(
    run: _Run,
    metric_id: str,
    spec: dict[str, Any],
    by_session: dict[int, list[tuple[float, float]]],
    segments: int,
    result: dict[str, Any],
) -> bool:
    failed = False
    worst: tuple[float, float, list[float]] | None = (
        None  # (growth, slope/1000, medians)
    )
    for index in run.sessions:
        series = by_session.get(index, [])
        parts = _split(series, segments)
        medians = [_med([v for _, v in part]) if part else math.nan for part in parts]
        drift = _tail_drift(series, segments) if series else None
        if not series or any(math.isnan(m) for m in medians) or drift is None:
            # 計算不能はtail_growthだけで合格させずfail
            result["status"] = "unreliable"
            result["judged"] = False
            if "metric_unreliable" not in result["reasons"]:
                result["reasons"].append("metric_unreliable")
            run.reasons.append(
                _reason(
                    "metric_unreliable",
                    "tail_slope_unavailable",
                    MIN_TAIL_SAMPLES,
                    metric_id,
                    index,
                )
            )
            failed = True
            continue
        tol = _tol(spec["tolerance"], medians[1] if len(medians) > 1 else medians[0])
        result["tolerance"] = {"abs": tol}
        slope, width = drift
        growth = medians[-1] - medians[math.ceil(segments / 2) - 1]
        drift_value = slope * width
        if worst is None or growth > worst[0]:
            worst = (growth, slope * 1000, medians)
        if (
            (growth > tol and drift_value > tol)
            or growth > 2 * tol
            or drift_value > 2 * tol
        ):
            run.reasons.append(
                _reason(
                    "tail_growth",
                    {"tail_growth": growth, "drift": drift_value},
                    tol,
                    metric_id,
                    index,
                )
            )
            if "tail_growth" not in result["reasons"]:
                result["reasons"].append("tail_growth")
            failed = True
    if worst is not None:
        # 報告するのは最も増えたSessionの値
        result["tail_growth"], result["tail_slope_per_1000_steps"] = worst[0], worst[1]
        result["segment_medians"] = worst[2]
    return failed


def _session_values(run: _Run, metric_id: str) -> dict[int, dict[str, float | None]]:
    # session別のpre_open / post_close(各phaseの最後の標本)
    table: dict[int, dict[str, float | None]] = {
        i: {"pre_open": None, "post_close": None} for i in run.sessions
    }
    for s in run.samples:
        if s["phase"] in ("pre_open", "post_close"):
            table[s["session_index"]][s["phase"]] = s["values"].get(metric_id)
    return table


def _judge_recovery(
    run: _Run,
    metric_id: str,
    spec: dict[str, Any],
    result: dict[str, Any],
    sessions_report: dict[int, dict[str, Any]],
) -> None:
    table = _session_values(run, metric_id)
    retained = float(run.retained.get(metric_id, 0))
    multi = len(run.sessions) > 1
    exempt = multi and metric_id.startswith(_FIRST_SESSION_EXEMPT_PREFIXES)
    if run.layer == "integration" and not metric_id.startswith("temp."):
        return  # 木のmetricは終了後に存在しない。孤児とtemp rootで判定する
    first = run.sessions[0] if run.sessions else 0
    missing = not_reclaimed = False
    for index in run.sessions:
        pre, post = table[index]["pre_open"], table[index]["post_close"]
        row = sessions_report[index]
        row["pre_open"][metric_id] = pre
        row["post_close"][metric_id] = post
        if pre is None or post is None:
            missing = True
            run.reasons.append(
                _reason(
                    "metric_unreliable",
                    "pre_open/post_close missing",
                    None,
                    metric_id,
                    index,
                )
            )
            continue
        # 宣言した保持があるmetricは先頭Sessionのpre_openを基準にし、宣言量を差し引く
        base = (
            table[first]["pre_open"]
            if retained and table[first]["pre_open"] is not None
            else pre
        )
        delta = post - float(base) - retained  # type: ignore[arg-type]
        # 許容幅も差分の基準にした値から求める(比較基準と許容幅を対応させる)
        limit = _tol(spec["recovery"], float(base))  # type: ignore[arg-type]
        row["recovery_delta_by_metric"][metric_id] = delta
        if exempt and index == first:
            row["first_session_growth"][metric_id] = post - pre
            continue
        if delta > limit:
            not_reclaimed = True
            run.reasons.append(_reason("not_reclaimed", delta, limit, metric_id, index))
    if missing or not_reclaimed:
        result["verdict"] = "fail"
    if missing:
        result["reasons"].append("metric_unreliable")
    if not_reclaimed:
        result["reasons"].append("not_reclaimed")
    _judge_baseline(run, metric_id, spec, result, table)


def _judge_baseline(
    run: _Run,
    metric_id: str,
    spec: dict[str, Any],
    result: dict[str, Any],
    table: dict[int, dict[str, float | None]],
) -> dict[str, Any] | None:
    if len(run.sessions) < 2:
        return None
    series = [
        table[i]["post_close"]
        for i in run.sessions[1:]
        if table[i]["post_close"] is not None
    ]
    if len(series) < 2:
        return None
    limit = _tol(spec["recovery"], float(series[0]))  # type: ignore[arg-type]
    delta = float(series[-1]) - float(series[0])  # type: ignore[arg-type]
    verdict = "pass"
    diffs = [float(b) - float(a) for a, b in zip(series, series[1:])]  # type: ignore[arg-type]
    if delta > limit:
        verdict = "fail"
        run.reasons.append(_reason("baseline_drift", delta, limit, metric_id))
    if (
        len(run.sessions) >= 4
        and len(diffs) >= 2
        and all(d > 0 for d in diffs)
        and sum(diffs) > limit / 2
    ):
        verdict = "fail"
        run.reasons.append(
            _reason("baseline_staircase", sum(diffs), limit / 2, metric_id)
        )
    if verdict == "fail":
        result["verdict"] = "fail"
    baseline = {
        "post_close_series": series,
        "delta_last_minus_second": delta,
        "verdict": verdict,
    }
    result.setdefault("_baseline", baseline)
    return baseline


# {
#   責務: [evaluate_run: samplesを判定しreport(resource-stability-report/v1)を作る]
#   処理: [妥当性検査, metricごとに成長・上限・回収・反復baselineを判定, 欠測と孤児を扱い, coverageとverdictを確定]
#   引数: [profile: 検証済みprofile, header/samples/footer: parse_samplesの結果, environment: runtime/code/host等の実行時情報, generated_at_utc: 生成時刻文字列]
#   戻り値: [reportの辞書]
#   エラー: [ProfileError: headerと層の不整合]
# }
def evaluate_run(
    profile: dict[str, Any],
    header: dict[str, Any],
    samples: list[dict[str, Any]],
    footer: dict[str, Any] | None = None,
    environment: Mapping[str, Any] | None = None,
    generated_at_utc: str = "",
) -> dict[str, Any]:
    run = _Run(profile, header, samples, footer)
    cfg = run.cfg
    needed_samples = int(cfg["segments"]) * int(cfg["min_samples_per_segment"])
    steady_by_session: dict[int, set[int]] = {}
    for s in samples:
        if run.is_steady(s):
            steady_by_session.setdefault(s["session_index"], set()).add(s["step"])
    # 1) 妥当性: 少ないdataで合格させない
    if run.layer != "integration" and len(run.sessions) < int(cfg["sessions"]):
        run.reasons.append(
            _reason("insufficient_sessions", len(run.sessions), cfg["sessions"])
        )
    for index in run.sessions:
        # Sessionごとに区間分割するため、各Sessionが必要数を満たすこと
        count = len(steady_by_session.get(index, ()))
        if count < needed_samples:
            run.reasons.append(
                _reason("insufficient_samples", count, needed_samples, session=index)
            )
    ids = list(dict.fromkeys([*run.metrics_spec, *header["metrics"]]))
    metrics: dict[str, Any] = {}
    sessions_report: dict[int, dict[str, Any]] = {
        i: {
            "index": i,
            "pre_open": {},
            "post_close": {},
            "recovery_delta_by_metric": {},
            "first_session_growth": {},
            "cleanup_latency_ms": None,
            "settle_timeout": False,
        }
        for i in run.sessions
    }
    for item in run.footer.get("sessions", []):
        if item.get("index") in sessions_report:
            sessions_report[item["index"]]["cleanup_latency_ms"] = item.get(
                "cleanup_latency_ms"
            )
            sessions_report[item["index"]]["settle_timeout"] = bool(
                item.get("settle_timeout", False)
            )
    baseline_drift: dict[str, Any] = {}
    coverage: dict[str, Any] = {
        "judged_metrics": [],
        "observed_only_metrics": [],
        "unsupported_required": [],
        "unsupported_optional": [],
        "not_judged_rules": [],
    }
    for metric_id in ids:
        info = header["metrics"].get(metric_id)
        unit, cls = metric_traits(metric_id) or ("count", "count")
        result: dict[str, Any] = {
            "status": "measured",
            "source": (info or {}).get(
                "source", "synthetic" if run.layer == "fast" else "process"
            ),
            "class": cls,
            "unit": unit,
            "required": run.required(metric_id),
            "judged": False,
            "verdict": "observed",
            "reasons": [],
            "tolerance": None,
        }
        if info is None or info.get("status") == "unsupported":
            reason = (info or {}).get("unsupported_reason", "no_counter")
            result.update(
                status="unsupported", unsupported_reason=reason, verdict="unsupported"
            )
            if result["required"]:
                result["verdict"] = "fail"
                run.reasons.append(
                    _reason("metric_unsupported", reason, "measured", metric_id)
                )
                coverage["unsupported_required"].append(metric_id)
            else:
                coverage["unsupported_optional"].append(metric_id)
            metrics[metric_id] = result
            continue
        _evaluate_metric(run, metric_id, result)
        spec = run.metrics_spec.get(metric_id)
        if result["judged"] and spec is not None and spec["class"] != "latency":
            _judge_recovery(run, metric_id, spec, result, sessions_report)
        baseline = result.pop("_baseline", None)
        if baseline is not None:
            baseline_drift[metric_id] = baseline
        if result["judged"]:
            coverage["judged_metrics"].append(metric_id)
        else:
            coverage["observed_only_metrics"].append(metric_id)
            if result["status"] == "unreliable" and result["required"]:
                coverage["unsupported_required"].append(metric_id)
        metrics[metric_id] = result
    _finish(run, metrics, coverage, sessions_report)
    return _report(
        run,
        metrics,
        sessions_report,
        baseline_drift,
        coverage,
        environment or {},
        generated_at_utc,
    )


def _finish(
    run: _Run,
    metrics: dict[str, Any],
    coverage: dict[str, Any],
    sessions_report: dict[int, dict[str, Any]],
) -> None:
    # 判定10: 全部unsupportedや設定ミスで通る経路を作らない
    judged = set(coverage["judged_metrics"])
    must = run.profile["layers"][run.layer]["must_judge"]
    required_ids = [*must.get("any", []), *must.get(run.host_os, [])]
    missing = [m for m in required_ids if m not in judged]
    if not judged or missing:
        run.reasons.append(
            _reason("no_judged_metrics", missing or "none", "must_judge")
        )
    if run.layer == "integration":
        coverage["not_judged_rules"].extend(
            [
                "baseline_drift: not_applicable (single observed session)",
                "recovery(process tree): not_applicable",
            ]
        )
        if int(run.footer.get("orphan_count", 0)) > 0:
            run.reasons.append(
                _reason("orphan_process", int(run.footer["orphan_count"]), 0)
            )
    # 観測対象の失敗・標本上限到達は成功扱いにしない(reportの利用者が終了コードを見なくても分かる)
    exit_code = run.footer.get("command_exit_code")
    if exit_code not in (None, 0):
        run.reasons.append(_reason("scenario_error", exit_code, 0))
    if run.footer.get("sampling_limit_reached"):
        run.reasons.append(
            _reason("scenario_error", "max_samples_reached", "max_samples")
        )
    # 判定9: runner側のcleanup latencyは報告のみ。settle未安定も免除しない
    for row in sessions_report.values():
        if row["settle_timeout"]:
            coverage["not_judged_rules"].append(
                f"settle_timeout: session {row['index']} (judged as measured)"
            )


def _report(
    run: _Run,
    metrics: dict[str, Any],
    sessions_report: dict[int, dict[str, Any]],
    baseline_drift: dict[str, Any],
    coverage: dict[str, Any],
    environment: Mapping[str, Any],
    generated_at_utc: str,
) -> dict[str, Any]:
    steady = [s for s in run.samples if run.is_steady(s)]
    interval = (
        run.cfg.get("interval_seconds")
        if run.layer == "integration"
        else run.cfg.get("sample_every_steps")
    )
    enforcement = run.profile["enforcement"][run.layer]
    verdict = "fail" if run.reasons else "pass"
    return _sanitize(
        {
            "schema": REPORT_SCHEMA,
            "generated_at_utc": generated_at_utc,
            "runtime": {
                "python": "unknown",
                "implementation": "unknown",
                "os": "unknown",
                "machine": "unknown",
                **dict(environment.get("runtime", {})),
            },
            "code": dict(environment.get("code", {"commit": "unknown"})),
            "host": {
                "profile_id": run.header["host_profile"],
                **dict(environment.get("host", {})),
            },
            "run": {
                "layer": run.layer,
                "run_profile": run.header["run_profile"],
                "scenario": run.header["scenario"],
                "seed": run.header.get("seed"),
                "enforcement": enforcement,
                "gate_effective": enforcement == "enforce",
                "duration_seconds": run.footer.get("duration_seconds"),
                # 実行したstep数(footerのtotal_steps)。不明ならnull(標本数はsampling.samples_totalで別に報告)
                "steps": run.footer.get("total_steps"),
                "sessions": len(run.sessions),
                "command_exit_code": run.footer.get("command_exit_code"),
            },
            "warmup": {
                "boundary_step": run.boundary_step,
                "boundary_seconds": run.boundary_seconds,
                "declared_fill_steps": run.fill_steps,
            },
            "sampling": {
                "mode": "seconds" if run.layer == "integration" else "steps",
                "interval": interval,
                "samples_total": len(run.samples),
                "samples_steady": len(steady),
            },
            "metrics": metrics,
            "sessions": [sessions_report[i] for i in run.sessions],
            "baseline_drift": baseline_drift,
            "coverage": coverage,
            "verdict": verdict,
            "reasons": run.reasons,
        }
    )


# NaN・無限大をJSONとして不正な値のまま出さずNoneにする(欠測を0へ読み替えない)
def _sanitize(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


# {
#   責務: [evaluate_rows: samples.jsonlの辞書列からreportまで一括で作る(evaluateサブコマンド・fast gate共通の入口)]
#   処理: [parse_samplesで検証, evaluate_runを呼ぶ]
#   引数: [profile: 検証済みprofile, rows: header+sample+footerの辞書列, environment/generated_at_utc: evaluate_runと同じ, judge: 判定関数(canary試験で差し替える)]
#   戻り値: [reportの辞書]
# }
def evaluate_rows(
    profile: dict[str, Any],
    rows: Sequence[Any],
    environment: Mapping[str, Any] | None = None,
    generated_at_utc: str = "",
    judge: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    header, samples, footer = parse_samples(rows)
    return (judge or evaluate_run)(
        profile, header, samples, footer, environment, generated_at_utc
    )
