"""3D ground electrical partition-shading normalization, dimensioning, and header helpers."""
from __future__ import annotations

from typing import Any

from pvsyst_auto.validation import finite_number

SHADING_FLAGS_LINEAR = "$000A0100"
SHADING_FLAGS_PARTITION = "$000A0300"
ELECTRICAL_SHADING_KEYS = frozenset((
    "mode", "partitions_x", "partitions_y", "electrical_effect_fraction", "auto_half_cut",
))
_PARTITION_ONLY_KEYS = ("partitions_x", "partitions_y", "electrical_effect_fraction", "auto_half_cut")
MIN_PARTITION_SPAN_M = 0.01
MAX_PARTITIONS = 1000


class _NormalizedShading(dict):
    """Internal dict marker so normalize_electrical_shading is idempotent on its own output."""


def _linear_defaults() -> dict[str, Any]:
    return _NormalizedShading({
        "mode": "linear",
        "partitions_x": None,
        "partitions_y": None,
        "electrical_effect_fraction": 1.0,
        "auto_half_cut": True,
    })


def _strict_pos_int(value: Any, label: str, *, maximum: int | None = None) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive strict integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} must be <= {maximum}")
    return value


def _strict_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a strict boolean")
    return value


def is_partition_shading_requested(raw: Any) -> bool:
    """Return True when raw electrical_shading requests partition mode; never raises on malformed input."""
    if raw is None:
        return False
    if isinstance(raw, str):
        return raw == "partition"
    if hasattr(raw, "model_dump"):
        try:
            raw = raw.model_dump()
        except Exception:
            return False
    if isinstance(raw, dict):
        return raw.get("mode", "partition") == "partition"
    return False


def normalize_electrical_shading(raw: Any) -> dict[str, Any]:
    """Validate and normalize an electrical_shading string, spec model, dict, or None."""
    if isinstance(raw, _NormalizedShading):
        return _NormalizedShading(raw)
    if raw is None:
        return _linear_defaults()
    if isinstance(raw, str):
        if raw == "linear":
            return _linear_defaults()
        if raw != "partition":
            raise ValueError("electrical_shading must be 'linear' or 'partition'")
        return _NormalizedShading({
            "mode": "partition",
            "partitions_x": None,
            "partitions_y": None,
            "electrical_effect_fraction": 1.0,
            "auto_half_cut": True,
        })
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        raise ValueError("electrical_shading must be 'linear', 'partition', or an options object")
    if unknown := sorted(set(raw) - ELECTRICAL_SHADING_KEYS):
        raise ValueError("Unknown electrical_shading parameter(s): " + ", ".join(unknown))
    mode = raw.get("mode", "partition")
    if mode not in ("linear", "partition"):
        raise ValueError("electrical_shading.mode must be 'linear' or 'partition'")
    if mode == "linear":
        disallowed = [key for key in _PARTITION_ONLY_KEYS if raw.get(key) is not None]
        if disallowed:
            raise ValueError(
                "electrical_shading mode='linear' cannot set partition-only option(s): " + ", ".join(disallowed)
            )
        return _linear_defaults()

    px_raw = raw.get("partitions_x")
    py_raw = raw.get("partitions_y")
    frac_raw = raw.get("electrical_effect_fraction")
    auto_hc_raw = raw.get("auto_half_cut")
    px = None if px_raw is None else _strict_pos_int(px_raw, "electrical_shading.partitions_x", maximum=MAX_PARTITIONS)
    py = None if py_raw is None else _strict_pos_int(py_raw, "electrical_shading.partitions_y", maximum=MAX_PARTITIONS)
    frac = 1.0 if frac_raw is None else finite_number(
        frac_raw, "electrical_shading.electrical_effect_fraction", minimum=0.0, maximum=1.0,
    )
    auto_hc = True if auto_hc_raw is None else _strict_bool(auto_hc_raw, "electrical_shading.auto_half_cut")
    return _NormalizedShading({
        "mode": "partition",
        "partitions_x": px,
        "partitions_y": py,
        "electrical_effect_fraction": float(frac),
        "auto_half_cut": auto_hc,
    })


def shading_header_kwargs(electrical_shading: Any = None) -> dict[str, Any]:
    """Return pv_shading_header keyword arguments (flags, frac_ombre_mod) for electrical_shading."""
    cfg = normalize_electrical_shading(electrical_shading)
    if cfg["mode"] == "partition":
        return {"flags": SHADING_FLAGS_PARTITION, "frac_ombre_mod": cfg["electrical_effect_fraction"]}
    return {"flags": SHADING_FLAGS_LINEAR, "frac_ombre_mod": 1.0}


def compute_table_partition_dims(
    *,
    long_shed_m: float,
    larg_shed_m: float,
    module_rows: int,
    module_columns: int,
    module_orient: str,
    is_tracker: bool = False,
    electrical_shading: Any = None,
    series_modules: int | None = None,
    is_half_cut: bool = False,
) -> tuple[float, float, int, int]:
    """Compute PVsyst (DXModule, DYModule, partitions_x, partitions_y) for one 3D table/tracker."""
    long_m = finite_number(long_shed_m, "long_shed_m", positive=True)
    larg_m = finite_number(larg_shed_m, "larg_shed_m", positive=True)
    n_rows = _strict_pos_int(module_rows, "module_rows")
    n_cols = _strict_pos_int(module_columns, "module_columns")
    if series_modules is not None:
        _strict_pos_int(series_modules, "series_modules")
    _strict_bool(is_tracker, "is_tracker")
    _strict_bool(is_half_cut, "is_half_cut")

    if module_orient in ("moPortrait", "portrait"):
        is_portrait = True
    elif module_orient in ("moLandscape", "moLandScape", "landscape"):
        is_portrait = False
    else:
        raise ValueError(f"Unsupported module_orient: {module_orient!r}")

    cfg = normalize_electrical_shading(electrical_shading)
    px_raw = cfg["partitions_x"]
    py_raw = cfg["partitions_y"]
    if series_modules is not None and n_cols < series_modules and n_rows >= 2:
        total_mods = n_rows * n_cols
        if py_raw is None:
            if not is_half_cut and total_mods <= series_modules:
                px = px_raw if px_raw is not None else 1
                py = 1
            else:
                raise ValueError(
                    f"Cannot automatically infer electrical partitions for {n_rows}x{n_cols} table "
                    f"with series_modules={series_modules} (cross-row strings); "
                    "set explicit electrical_shading.partitions_y (and partitions_x if needed)"
                )
        else:
            px = px_raw if px_raw is not None else 1
            py = py_raw
    else:
        px = (
            px_raw
            if px_raw is not None
            else (max(1, n_cols // series_modules) if series_modules is not None else 1)
        )
        py = (
            py_raw
            if py_raw is not None
            else (n_rows * 2 if (is_portrait and cfg["auto_half_cut"] and is_half_cut) else n_rows)
        )
    if is_tracker:
        dx_mod = long_m / py
        dy_mod = larg_m / px
    else:
        dx_mod = long_m / px
        dy_mod = larg_m / py
    if dx_mod < MIN_PARTITION_SPAN_M - 1e-12 or dy_mod < MIN_PARTITION_SPAN_M - 1e-12:
        raise ValueError(
            f"Partition rectangle ({dx_mod:.4f}m x {dy_mod:.4f}m) is smaller than {MIN_PARTITION_SPAN_M}m"
        )
    return (dx_mod, dy_mod, px, py)


def build_electrical_shading_summary(
    electrical_shading: Any,
    *,
    long_shed_m: float,
    larg_shed_m: float,
    module_rows: int,
    module_columns: int,
    module_orient: str,
    is_tracker: bool = False,
    series_modules: int | None = None,
    is_half_cut: bool = False,
) -> dict[str, Any]:
    """Build structured electrical_shading metadata for utility_summary when 3D scene is present."""
    cfg = normalize_electrical_shading(electrical_shading)
    hdr = shading_header_kwargs(cfg)
    summary: dict[str, Any] = {
        "mode": cfg["mode"],
        "flags": hdr["flags"],
        "electrical_effect_fraction": hdr["frac_ombre_mod"],
        "is_half_cut": bool(is_half_cut),
        "auto_half_cut": cfg["auto_half_cut"],
    }
    if cfg["mode"] == "partition":
        dx_mod, dy_mod, px, py = compute_table_partition_dims(
            long_shed_m=long_shed_m,
            larg_shed_m=larg_shed_m,
            module_rows=module_rows,
            module_columns=module_columns,
            module_orient=module_orient,
            is_tracker=is_tracker,
            electrical_shading=cfg,
            series_modules=series_modules,
            is_half_cut=is_half_cut,
        )
        summary.update({
            "partitions_x": px,
            "partitions_y": py,
            "dx_module_m": round(dx_mod, 3),
            "dy_module_m": round(dy_mod, 3),
        })
    return summary


def summarize_utility_electrical_shading(
    electrical_shading: Any,
    *,
    field_type: str,
    cad_resolved: dict[str, Any] | None,
    ground_3d_layout: dict[str, Any] | None,
    module_width_m: float,
    module_length_m: float,
    module_orient: str,
    spacing_x_m: float,
    spacing_y_m: float,
    module_rows: int,
    module_columns: int,
    series_modules: int,
    is_half_cut: bool,
) -> dict[str, Any]:
    """Compute representative-table electrical_shading metadata for utility_summary."""
    is_trk = field_type in ("TrackHorizNSAxis", "UnlimTracking")
    if cad_resolved is not None and cad_resolved["mode"] == "pvc":
        rep_tbl = cad_resolved["first_table"]
    elif isinstance(ground_3d_layout, dict) and ground_3d_layout.get("tables"):
        rep_tbl = ground_3d_layout["tables"][0]
    else:
        if is_trk:
            d_across, d_along = (module_length_m, module_width_m) if module_orient == "moPortrait" else (module_width_m, module_length_m)
            long_m = module_rows * d_across + max(0, module_rows - 1) * spacing_y_m
            larg_m = module_columns * d_along + max(0, module_columns - 1) * spacing_x_m
        else:
            d_across, d_along = (module_width_m, module_length_m) if module_orient == "moPortrait" else (module_length_m, module_width_m)
            long_m = module_columns * d_across + max(0, module_columns - 1) * spacing_x_m
            larg_m = module_rows * d_along + max(0, module_rows - 1) * spacing_y_m
        rep_tbl = {
            "long_shed_m": long_m,
            "larg_shed_m": larg_m,
            "module_rows": module_rows,
            "module_columns": module_columns,
            "module_orient": module_orient,
        }
    return build_electrical_shading_summary(
        electrical_shading,
        long_shed_m=float(rep_tbl["long_shed_m"]),
        larg_shed_m=float(rep_tbl["larg_shed_m"]),
        module_rows=int(rep_tbl["module_rows"]),
        module_columns=int(rep_tbl["module_columns"]),
        module_orient=str(rep_tbl.get("module_orient", module_orient)),
        is_tracker=is_trk,
        series_modules=series_modules,
        is_half_cut=is_half_cut,
    )
