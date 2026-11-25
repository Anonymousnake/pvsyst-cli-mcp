"""Public Python adapter for installed PVsyst; no bundled simulator."""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
from pathlib import Path
import sys

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
    "pvsyst_auto.project.fileio": ("pvsyst_auto.project.paths",),
    "pvsyst_auto.project.native_economics": ("pvsyst_auto.project.variant_updates",),
    "pvsyst_auto.project.orientation_convert": ("pvsyst_auto.project.tracking",),
    "pvsyst_auto.project.seasonal": ("pvsyst_auto.project.bifacial",),
    "pvsyst_auto.project.sheds": ("pvsyst_auto.project.bifacial", "pvsyst_auto.project.tracking"),
    "pvsyst_auto.project.variant_readback": ("pvsyst_auto.project.variant_updates", "pvsyst_auto.project.variants"),
    "pvsyst_auto.project.variant_topology": ("pvsyst_auto.project.circuit",),
    "pvsyst_auto.project.variant_writer": ("pvsyst_auto.project.variant_updates",),
}


class _LegacySubmoduleFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname: str, path=None, target=None):
        if fullname not in _LEGACY_MODULE_ALIASES:
            return None
        rel = Path(*fullname.split(".")[1:]).with_suffix(".py")
        if (_PACKAGE_ROOT / rel).is_file():
            return None
        return importlib.machinery.ModuleSpec(fullname, self)

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        for target_name in _LEGACY_MODULE_ALIASES[module.__name__]:
            target = importlib.import_module(target_name)
            for key, value in target.__dict__.items():
                if not key.startswith("__"):
                    module.__dict__.setdefault(key, value)


if not any(isinstance(f, _LegacySubmoduleFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _LegacySubmoduleFinder())
