"""Public Python adapter for installed PVsyst; no bundled simulator."""
from __future__ import annotations

from decimal import Decimal
import importlib
import importlib.abc
import importlib.machinery
from pathlib import Path
import re
import sys
import types

from .native.cli import PVsystCLI, PVsystError

__all__ = ["PVsystCLI", "PVsystError"]

_PACKAGE_ROOT = Path(__file__).resolve().parent

_LEGACY_MODULE_ALIASES: dict[str, tuple[str, ...]] = {
    "pvsyst_auto.components.battery": ("pvsyst_auto.components.builder",),
    "pvsyst_auto.components.commercial": ("pvsyst_auto.components.builder",),
    "pvsyst_auto.components.curves": ("pvsyst_auto.components.editor",),
    "pvsyst_auto.components.generator": ("pvsyst_auto.components.editor",),
    "pvsyst_auto.components.identity": ("pvsyst_auto.components.editor",),
    "pvsyst_auto.components.pan": ("pvsyst_auto.components.editor",),
    "pvsyst_auto.energy.ac": ("pvsyst_auto.energy.system",),
    "pvsyst_auto.energy.ageing": ("pvsyst_auto.energy.dc",),
    "pvsyst_auto.energy.auxiliaries": ("pvsyst_auto.energy.system",),
    "pvsyst_auto.energy.availability": ("pvsyst_auto.energy.profiles",),
    "pvsyst_auto.energy.iam": ("pvsyst_auto.energy.dc",),
    "pvsyst_auto.energy.inverter_temperature": ("pvsyst_auto.energy.system",),
    "pvsyst_auto.energy.load": ("pvsyst_auto.energy.profiles",),
    "pvsyst_auto.energy.mismatch": ("pvsyst_auto.energy.dc",),
    "pvsyst_auto.energy.peak": ("pvsyst_auto.energy.storage",),
    "pvsyst_auto.energy.power": ("pvsyst_auto.energy.system",),
    "pvsyst_auto.energy.power_factor": ("pvsyst_auto.energy.system",),
    "pvsyst_auto.energy.soiling": ("pvsyst_auto.energy.dc",),
    "pvsyst_auto.energy.spectral": ("pvsyst_auto.energy.dc",),
    "pvsyst_auto.energy.tou_planner": ("pvsyst_auto.energy.storage",),
    "pvsyst_auto.energy.unavailability": ("pvsyst_auto.energy.profiles",),
    "pvsyst_auto.native._prep": ("pvsyst_auto.native.headless",),
    "pvsyst_auto.native._workspace_ini": ("pvsyst_auto.native._winlaunch",),
    "pvsyst_auto.native.grid_limit": ("pvsyst_auto.native.load",),
    "pvsyst_auto.native.horizon": ("pvsyst_auto.native.load",),
    "pvsyst_auto.native.p50": ("pvsyst_auto.native.load",),
    "pvsyst_auto.project.fileio": ("pvsyst_auto.project.paths",),
    "pvsyst_auto.project.native_economics": ("pvsyst_auto.project.variant_updates",),
    "pvsyst_auto.project.orientation_convert": ("pvsyst_auto.project.tracking",),
    "pvsyst_auto.project.seasonal": ("pvsyst_auto.project.bifacial",),
    "pvsyst_auto.project.sheds": ("pvsyst_auto.project.bifacial", "pvsyst_auto.project.tracking"),
    "pvsyst_auto.project.variant_readback": ("pvsyst_auto.project.variant_updates", "pvsyst_auto.project.variants"),
    "pvsyst_auto.project.variant_topology": ("pvsyst_auto.project.circuit",),
    "pvsyst_auto.project.variant_writer": ("pvsyst_auto.project.variant_updates",),
    "pvsyst_auto.scene.ground": ("pvsyst_auto.scene.reader",),
    "pvsyst_auto.scene.state": ("pvsyst_auto.scene.reader",),
    "pvsyst_auto.weather.csv": ("pvsyst_auto.weather.preparation",),
}

_LEGACY_ATTR_RENAMES: dict[str, dict[str, tuple[str, str]]] = {
    "pvsyst_auto.components.battery": {
        "ALIASES": ("pvsyst_auto.components.builder", "BTR_CURVE_ALIASES"),
        "KNOWN_TAGS": ("pvsyst_auto.components.builder", "BTR_KNOWN_TAGS"),
    },
    "pvsyst_auto.components.commercial": {
        "PATH": ("pvsyst_auto.components.builder", "COMMERCIAL_PATH"),
    },
    "pvsyst_auto.components.identity": {
        "ComponentStore": ("pvsyst_auto.components.store", "ComponentStore"),
        "electrical_inventory": ("pvsyst_auto.components.store", "electrical_inventory"),
    },
    "pvsyst_auto.components.pan": {
        "ALIASES": ("pvsyst_auto.components.editor", "PAN_ALIASES"),
        "CANONICAL": ("pvsyst_auto.components.editor", "PAN_CANONICAL"),
        "DIMENSIONS": ("pvsyst_auto.components.editor", "PAN_DIMENSIONS"),
    },
    "pvsyst_auto.energy.ageing": {
        "FLAGS2_RE": ("pvsyst_auto.energy.dc", "_HEX_FLAG"),
    },
    "pvsyst_auto.energy.auxiliaries": {
        "POWER_FIELDS": ("pvsyst_auto.energy.system", "_AUX_POWER_FIELDS"),
        "PROFILE_FIELDS": ("pvsyst_auto.energy.system", "_AUX_PROFILE_FIELDS"),
        "OWNED_FIELDS": ("pvsyst_auto.energy.system", "_AUX_OWNED"),
        "STEPS": ("pvsyst_auto.energy.system", "_AUX_STEPS"),
    },
    "pvsyst_auto.energy.inverter_temperature": {
        "FIELDS": ("pvsyst_auto.energy.system", "_TEMP_FIELDS"),
        "FLAGS": ("pvsyst_auto.energy.system", "_TEMP_FLAGS"),
        "KNOWN": ("pvsyst_auto.energy.system", "_TEMP_KNOWN"),
    },
    "pvsyst_auto.energy.mismatch": {
        "FIELD": ("pvsyst_auto.energy.dc", "_MISMATCH_FIELD"),
        "ANCHOR": ("pvsyst_auto.energy.dc", "_MISMATCH_ANCHOR"),
        "SIBLING": ("pvsyst_auto.energy.dc", "_MISMATCH_SIBLING"),
    },
    "pvsyst_auto.energy.power": {
        "COMMON": ("pvsyst_auto.energy.system", "_PWR_COMMON"),
        "PF_OWN": ("pvsyst_auto.energy.system", "_PWR_PF_OWN"),
        "GRID_OWN": ("pvsyst_auto.energy.system", "_PWR_GRID_OWN"),
        "ORDER": ("pvsyst_auto.energy.system", "_PWR_ORDER"),
        "PF_BITS": ("pvsyst_auto.energy.system", "_PWR_PF_BITS"),
        "PF_MODE_BITS": ("pvsyst_auto.energy.system", "_PWR_PF_MODE_BITS"),
        "PNOM_BITS": ("pvsyst_auto.energy.system", "_PNOM_BITS"),
        "LEGAL": ("pvsyst_auto.energy.system", "_PWR_LEGAL"),
    },
    "pvsyst_auto.energy.power_factor": {
        "FIELDS": ("pvsyst_auto.energy.system", "_PF_FIELDS"),
        "PF_BITS": ("pvsyst_auto.energy.system", "_PF_BITS"),
        "PNOM_BITS": ("pvsyst_auto.energy.system", "_PNOM_BITS"),
        "ALLOWED_BITS": ("pvsyst_auto.energy.system", "_PWR_PF_BITS"),
        "KNOWN": ("pvsyst_auto.energy.system", "_PF_KNOWN"),
    },
    "pvsyst_auto.native.grid_limit": {
        "_read": ("pvsyst_auto.native.load", "_grid_limit_read"),
    },
    "pvsyst_auto.native.horizon": {
        "ROOT": ("pvsyst_auto.native.load", "_HORIZON_ROOT"),
        "END": ("pvsyst_auto.native.load", "_HORIZON_END"),
        "PROFILE": ("pvsyst_auto.native.load", "_HORIZON_PROFILE"),
        "PROFILE_END": ("pvsyst_auto.native.load", "_HORIZON_PROFILE_END"),
        "MAIN": ("pvsyst_auto.native.load", "_HORIZON_MAIN"),
        "SLOTS": ("pvsyst_auto.native.load", "_HORIZON_SLOTS"),
        "DERIVED": ("pvsyst_auto.native.load", "_HORIZON_DERIVED"),
        "OUTER": ("pvsyst_auto.native.load", "_HORIZON_OUTER"),
        "INNER": ("pvsyst_auto.native.load", "_HORIZON_INNER"),
        "POINT": ("pvsyst_auto.native.load", "_HORIZON_POINT"),
        "LAST_COMPILE": ("pvsyst_auto.native.load", "_HORIZON_LAST_COMPILE"),
        "_number": ("pvsyst_auto.native.load", "_horizon_number"),
        "_locate": ("pvsyst_auto.native.load", "_horizon_locate"),
        "_read": ("pvsyst_auto.native.load", "_horizon_read"),
        "_check_points": ("pvsyst_auto.native.load", "_horizon_check_points"),
        "_check_name": ("pvsyst_auto.native.load", "_horizon_check_name"),
        "_body": ("pvsyst_auto.native.load", "_horizon_body"),
    },
    "pvsyst_auto.native.p50": {
        "HEAD": ("pvsyst_auto.native.load", "_P50_HEAD"),
        "END": ("pvsyst_auto.native.load", "_P50_END"),
        "PLOTS": ("pvsyst_auto.native.load", "_P50_PLOTS"),
        "ROOT_TYPE": ("pvsyst_auto.native.load", "_P50_ROOT_TYPE"),
        "ROOT_FLAGS": ("pvsyst_auto.native.load", "_P50_ROOT_FLAGS"),
        "READ_FLAGS": ("pvsyst_auto.native.load", "_P50_READ_FLAGS"),
        "ORDER": ("pvsyst_auto.native.load", "_P50_ORDER"),
        "INTS": ("pvsyst_auto.native.load", "_P50_INTS"),
        "BOOLS": ("pvsyst_auto.native.load", "_P50_BOOLS"),
        "VARIAB": ("pvsyst_auto.native.load", "_P50_VARIAB"),
        "PLOT_KIND": ("pvsyst_auto.native.load", "_P50_PLOT_KIND"),
        "_step": ("pvsyst_auto.native.load", "_p50_step"),
        "_label": ("pvsyst_auto.native.load", "_p50_label"),
        "_validate": ("pvsyst_auto.native.load", "_p50_validate"),
        "_read": ("pvsyst_auto.native.load", "_p50_read"),
    },
    "pvsyst_auto.project.seasonal": {
        "SEASONAL": ("pvsyst_auto.project.bifacial", "SEASONAL_FIELDS"),
        "BOOLS": ("pvsyst_auto.project.bifacial", "SEASONAL_BOOLS"),
    },
    "pvsyst_auto.project.sheds": {
        "TYPES": ("pvsyst_auto.project.bifacial", "SHED_TYPES"),
        "WRITABLE": ("pvsyst_auto.project.bifacial", "SHED_WRITABLE"),
        "ANGLES": ("pvsyst_auto.project.bifacial", "_SHED_ANGLES"),
        "COUNTS": ("pvsyst_auto.project.bifacial", "_SHED_COUNTS"),
        "AVER": ("pvsyst_auto.project.bifacial", "SEASONAL_BOOLS"),
        "MAX_COUNT": ("pvsyst_auto.project.bifacial", "_SHED_MAX_COUNT"),
        "ANGLE_LIMITS": ("pvsyst_auto.project.bifacial", "_SHED_ANGLE_LIMITS"),
        "STEP_ANGLE": ("pvsyst_auto.project.tracking", "STEP"),
    },
    "pvsyst_auto.project.variant_readback": {
        "SHED_TYPES": ("pvsyst_auto.project.bifacial", "SHED_TYPES"),
        "SHED_FIELDS": ("pvsyst_auto.project.bifacial", "SHED_WRITABLE"),
        "TRACKING_FIELDS": ("pvsyst_auto.project.tracking", "WRITABLE"),
        "ac_cabling_state": ("pvsyst_auto.energy.system", "ac_cabling_state"),
        "ageing_model_state": ("pvsyst_auto.energy.dc", "ageing_model_state"),
        "auxiliary_state": ("pvsyst_auto.energy.system", "auxiliary_state"),
        "bifacial_state": ("pvsyst_auto.project.bifacial", "bifacial_state"),
        "dc_cabling_state": ("pvsyst_auto.energy.dc", "dc_cabling_state"),
        "grid_storage_state": ("pvsyst_auto.energy.storage", "grid_storage_state"),
        "iam_model_state": ("pvsyst_auto.energy.dc", "iam_model_state"),
        "inverter_temperature_state": ("pvsyst_auto.energy.system", "inverter_temperature_state"),
        "peak_shaving_state": ("pvsyst_auto.energy.storage", "peak_shaving_state"),
        "power_controls_state": ("pvsyst_auto.energy.system", "power_controls_state"),
        "power_factor_state": ("pvsyst_auto.energy.system", "power_factor_state"),
        "shading_state": ("pvsyst_auto.scene.reader", "shading_state"),
        "soiling_state": ("pvsyst_auto.energy.dc", "soiling_state"),
        "string_mismatch_state": ("pvsyst_auto.energy.dc", "string_mismatch_state"),
        "system_unavail_state": ("pvsyst_auto.energy.profiles", "system_unavail_state"),
    },
    "pvsyst_auto.project.variant_topology": {
        "grid_inventory": ("pvsyst_auto.project.circuit", "inventory"),
        "strip_result_cache": ("pvsyst_auto.native.headless", "strip_result_cache"),
    },
}

_LEGACY_EXTRA_CONSTANTS: dict[str, dict[str, object]] = {
    "pvsyst_auto.energy.ageing": {
        "YEAR_OBJECT": "pvAgeing",
        "YEAR_OBJECT_OWNER": "PVObject_Ageing",
        "MAIN_OBJECT": "pvMainArray",
        "MAIN_FLAGS2_ENABLED_VALUE": 0x10082A,
        "MAIN_FLAGS2_DISABLED_VALUE": 0x82A,
        "MAIN_ENABLE_BIT": 0x100000,
        "MODEL_KEYS": ("simulation_year", "annual_module_degradation_percent"),
        "OPTIONAL_KEYS": ("keep_mc_values", "modules_rms_year_isc_percent", "modules_rms_year_voc_percent"),
    },
    "pvsyst_auto.energy.auxiliaries": {
        "POWER_FIELDS_KW": ("operating_kw", "night_kw"),
        "PROFILE_REQUEST_KEYS": {
            "mode",
            "operating_kw",
            "night_kw",
            "operating_threshold_kw",
            "proportional_w_per_kw",
            "proportional_threshold_kw",
        },
        "P50_MARKER": "P50_P90=",
    },
    "pvsyst_auto.energy.iam": {
        "ALLOWED_FLAGS2": (0x20, 0x00),
        "ASHRAE_KEYS": ("Flags", "IAMMode", "B0_IAM"),
        "USER_KEYS": ("Flags", "IAMMode"),
        "CACHE_KEYS": ("NPtsMax", "NPtsEff", "LastCompile", "Mode"),
    },
    "pvsyst_auto.energy.inverter_temperature": {
        "FLAG_MASK": 0xC000,
    },
    "pvsyst_auto.energy.power_factor": {
        "MODES": ("none", "constant", "monthly"),
        "MONTHS": 12,
    },
    "pvsyst_auto.project.seasonal": {
        "REQUIRED": ("summer_tilt", "winter_tilt", "winter_months"),
        "ALLOWED": {"summer_tilt", "winter_tilt", "winter_months", "orientation_name"},
        "MASK": re.compile(r"\$[0-9A-Fa-f]{1,4}"),
    },
    "pvsyst_auto.project.sheds": {
        "FLAGS": ("UsesElectricalEffect",),
        "STEP_LENGTH": Decimal("0.001"),
        "POSITIVE_LENGTHS": ("Pitch", "CollectorWidth", "CellWidth"),
        "NONNEGATIVE_LENGTHS": ("InactiveBandWidth1", "InactiveBandWidth2"),
        "FACTOR_LIMITS": (Decimal("0"), Decimal("1")),
    },
}


class _LegacyAliasModule(types.ModuleType):
    """Module view forwarding attribute reads/writes/patches to backing consolidated modules."""

    def _sync(self):
        d = object.__getattribute__(self, "__dict__")
        owners = d.get("_legacy_attr_owners")
        if owners:
            for key, (target_mod, target_attr) in owners.items():
                if hasattr(target_mod, target_attr):
                    d[key] = getattr(target_mod, target_attr)
                else:
                    d.pop(key, None)
        return d

    def __getattribute__(self, name: str):
        if name == "__dict__":
            return object.__getattribute__(self, "_sync")()
        if not name.startswith("__") and name not in ("_legacy_attr_owners", "_sync"):
            d = object.__getattribute__(self, "__dict__")
            owners = d.get("_legacy_attr_owners")
            if owners is not None and name in owners:
                target_mod, target_attr = owners[name]
                if hasattr(target_mod, target_attr):
                    val = getattr(target_mod, target_attr)
                    d[name] = val
                    return val
                d.pop(name, None)
                raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")
        return super().__getattribute__(name)

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        owners = object.__getattribute__(self, "__dict__").get("_legacy_attr_owners")
        if owners is not None and name in owners:
            target_mod, target_attr = owners[name]
            setattr(target_mod, target_attr, value)

    def __delattr__(self, name: str) -> None:
        d = object.__getattribute__(self, "__dict__")
        owners = d.get("_legacy_attr_owners")
        if owners is not None and name in owners:
            target_mod, target_attr = owners[name]
            d.pop(name, None)
            if hasattr(target_mod, target_attr):
                delattr(target_mod, target_attr)
                return
            raise AttributeError(name)
        super().__delattr__(name)


class _LegacySubmoduleFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    _PVSYST_LEGACY_FINDER = True

    def find_spec(self, fullname: str, path=None, target=None):
        if fullname not in _LEGACY_MODULE_ALIASES:
            return None
        parts = fullname.split(".")[1:]
        if path is not None:
            expected_parent = (_PACKAGE_ROOT / Path(*parts[:-1])).resolve()
            resolved_entries: list[Path] = []
            for entry in path:
                try:
                    resolved_entries.append(Path(entry).resolve())
                except OSError:
                    continue
            if expected_parent not in resolved_entries:
                return None
            if any(
                (entry_dir / f"{parts[-1]}.py").is_file() or (entry_dir / parts[-1] / "__init__.py").is_file()
                for entry_dir in resolved_entries
            ):
                return None
        elif (_PACKAGE_ROOT / Path(*parts).with_suffix(".py")).is_file():
            return None
        primary_rel = Path(*_LEGACY_MODULE_ALIASES[fullname][0].split(".")[1:]).with_suffix(".py")
        origin_path = (_PACKAGE_ROOT / primary_rel).resolve()
        origin = str(origin_path) if origin_path.is_file() else None
        spec = importlib.machinery.ModuleSpec(fullname, self, origin=origin)
        spec.has_location = origin is not None
        return spec

    def create_module(self, spec):
        return _LegacyAliasModule(spec.name)

    def exec_module(self, module) -> None:
        owners: dict[str, tuple[types.ModuleType, str]] = {}
        seen: set[str] = set()
        for target_name in _LEGACY_MODULE_ALIASES[module.__name__]:
            target = importlib.import_module(target_name)
            for key, value in target.__dict__.items():
                if not key.startswith("__") and key not in seen:
                    seen.add(key)
                    module.__dict__[key] = value
                    owners[key] = (target, key)
        for attr_name, (target_mod_name, target_attr) in _LEGACY_ATTR_RENAMES.get(module.__name__, {}).items():
            target_mod = importlib.import_module(target_mod_name)
            module.__dict__[attr_name] = getattr(target_mod, target_attr)
            owners[attr_name] = (target_mod, target_attr)
        for attr_name, value in _LEGACY_EXTRA_CONSTANTS.get(module.__name__, {}).items():
            module.__dict__[attr_name] = value
            owners.pop(attr_name, None)
        module.__dict__["_legacy_attr_owners"] = owners


sys.meta_path[:] = [f for f in sys.meta_path if not getattr(f, "_PVSYST_LEGACY_FINDER", False)]
sys.meta_path.append(_LegacySubmoduleFinder())
