"""実装済みABIと分離して、#661の未実装target配置を検査する。"""

from __future__ import annotations

from typing import Any


def check_target_architecture(manifest: dict[str, Any]) -> list[str]:
    """旧v1は受理し、targetがある場合だけ配置と未確認gateを照合する。"""
    if "target_architecture" not in manifest:
        return []
    value = manifest["target_architecture"]
    if not isinstance(value, dict):
        return ["target_architecture must be an object"]
    errors: list[str] = []
    expected: dict[str, object] = {
        "schema": "kadoka-target-architecture/v1",
        "status": "planned",
        "decision_issue": 661,
    }
    for field, selected in expected.items():
        if type(value.get(field)) is not type(selected) or value[field] != selected:
            errors.append(f"target_architecture.{field} must be {selected!r}")
    sections: dict[str, dict[str, object]] = {
        "gui": {
            "language": "cpp",
            "framework": "qt6_widgets",
            "embedded_webview": False,
            "issue": 664,
        },
        "resident_core": {"language": "rust", "process": "separate", "issue": 665},
        "bridge": {
            "control": "versioned_local_ipc",
            "frame": "frame_ref_lease",
            "issue": 666,
        },
        "python_worker": {"language": "python", "required_for_core": False},
        "capture": {
            "language": "cpp",
            "status": "existing_compatibility",
            "abi_version": 1,
        },
        "safety": {
            "authority": "resident_core",
            "ui_independent": True,
            "stop_bypasses_work_queue": True,
            "issue": 667,
        },
        "compatibility": {
            "tkinter": True,
            "python_cpp_abi_v1": True,
            "existing_windows_e2e": True,
        },
        "distribution": {
            "status": "unverified",
            "qt_linkage": "dynamic",
            "gpl_only_modules": False,
        },
    }
    for name, fields in sections.items():
        section = value.get(name)
        if not isinstance(section, dict):
            errors.append(f"target_architecture.{name} must be an object")
            continue
        for field, selected in fields.items():
            if (
                type(section.get(field)) is not type(selected)
                or section[field] != selected
            ):
                errors.append(
                    f"target_architecture.{name}.{field} must be {selected!r}"
                )
    distribution = value.get("distribution")
    gates = distribution.get("gate_issues") if isinstance(distribution, dict) else None
    if (
        not isinstance(gates, list)
        or any(type(n) is not int for n in gates)
        or len(gates) != 3
        or set(gates) != {149, 228, 663}
    ):
        errors.append("target distribution must retain unverified gates #149/#228/#663")
    return errors
