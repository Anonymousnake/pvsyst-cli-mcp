"""Guarded project copies, lifecycle operations and bounded PVsyst variant edits."""
from __future__ import annotations

import hashlib
import difflib
import itertools
import json
import math
import os
import re
import threading
import uuid
from pathlib import Path

from pvsyst_components import ComponentStore, FIELD, MAX_PROJECT_BYTES
from pvsyst_paths import project_path, project_root
from pvsyst_generator import state as generator_state, replace_generator
from pvsyst_grid import inventory as grid_inventory, resize_capacity, circuit_nodes, require_ordinary_mppt, set_power_sharing, set_independent_mppt
from pvsyst_component_editor import objects, replace_value
from pvsyst_soiling import soiling_state, replace_soiling
from pvsyst_grid_limit_native import grid_limit_state, replace_grid_limit
from pvsyst_auxiliaries import auxiliary_state, replace_auxiliaries
from pvsyst_seasonal import replace_seasonal
from pvsyst_horizon_native import horizon_state, replace_horizon
from pvsyst_inverter_temperature import inverter_temperature_state, replace_inverter_temperature
from pvsyst_power_factor import power_factor_state, replace_power_factor
from pvsyst_string_mismatch import replace_string_mismatch, string_mismatch_state
from pvsyst_ageing import ageing_model_state, replace_ageing_model
from pvsyst_iam import iam_model_state, replace_iam_model
from pvsyst_p50_native import p50_state, replace_p50
from pvsyst_power_controls import power_controls_state, replace_power_controls

FIELDS = {"PAN": "PVModule", "OND": "GInverter", "BTR": "BatteryFile", "GEN": "GensetFile"}
STARTS = {"pvSubArray": "PVObject_=pvSubArray", "pvSystem": "PVObject_System=pvSystem"}
MAX_ARCHIVE_MEMBERS = 128
EMPTY_SHADING_FLAGS = ("$00", "$00020000", "$00080000")


class VariantStore:
    def __init__(self, workspace: str | os.PathLike, components: ComponentStore,
                 lock: threading.RLock | None = None):
        self.workspace = Path(workspace).resolve(strict=True)
        self.components = components
        self.lock = lock or threading.RLock()

    def _root(self) -> Path:
        return project_root(self.workspace)

    def _path(self, project: str, variant: str | None = None) -> Path:
        return project_path(self.workspace, project, variant)

    @staticmethod
    def _check_size(data: bytes) -> None:
        if len(data) > MAX_PROJECT_BYTES:
            raise ValueError("Project or variant exceeds 32 MB")

    @staticmethod
    def _belongs_to_project(path: Path, project: str) -> bool:
        """Exact stems plus attached PRJ/VC sidecars, excluding other dotted projects."""
        stem = project[:-4].casefold()
        if path.stem.casefold() == stem:
            return True
        if re.fullmatch(r"\.(?:PRJ|VC[A-Za-z0-9]+)", path.suffix, re.I):
            return False
        return bool(path.name.casefold().startswith(stem + ".") and re.fullmatch(
            r"(?:PRJ|VC[A-Za-z0-9]+)\..+", path.name[len(project[:-4]) + 1:], re.I))

    @staticmethod
    def _read(path: Path) -> bytes:
        if not path.is_file():
            raise FileNotFoundError(path.name)
        if path.stat().st_size > MAX_PROJECT_BYTES:
            raise ValueError("Project or variant exceeds 32 MB")
        data = path.read_bytes()
        VariantStore._check_size(data)
        return data

    def _check_project(self, project: str) -> None:
        self._project_text(self._read(self._path(project)))

    @staticmethod
    def _project_text(data: bytes) -> None:
        VariantStore._check_size(data)
        try:
            lines = data.decode("utf-8-sig").splitlines()
        except UnicodeDecodeError as exc:
            raise ValueError("Project must be UTF-8 text") from exc
        if (not lines or lines[0].strip() != "PVObject_=pvProject"
                or lines[-1].strip() != "End of PVObject pvProject"
                or "\x00" in "".join(lines)):
            raise ValueError("Project is not a supported pvProject file")

    @staticmethod
    def _text(data: bytes) -> tuple[str, list[str]]:
        VariantStore._check_size(data)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Variant must be UTF-8 text") from exc
        if "\x00" in text or not text.startswith("PVObject_=pvVCalcul"):
            raise ValueError("Variant is not a supported pvVCalcul file")
        lines = text.splitlines(keepends=True)
        if not lines or lines[-1].strip() != "End of PVObject pvVCalcul":
            raise ValueError("Variant has no pvVCalcul closing tag")
        return text, lines

    @staticmethod
    def _sections(lines: list[str]) -> tuple[dict[int, tuple[int, int]], tuple[int, int] | None]:
        arrays = {}
        system = None
        active = None
        for index, line in enumerate(lines):
            value = line.strip()
            if value in STARTS.values():
                if active is not None:
                    raise ValueError("Nested component sections are not supported")
                active = ("pvSubArray" if value == STARTS["pvSubArray"] else "pvSystem", index)
            elif value in ("End of PVObject pvSubArray", "End of PVObject pvSystem"):
                kind = value.removeprefix("End of PVObject ")
                if active is None or active[0] != kind:
                    raise ValueError("Unmatched component section")
                bounds = (active[1], index)
                if kind == "pvSystem":
                    if system is not None:
                        raise ValueError("Multiple system sections")
                    system = bounds
                else:
                    matches = VariantStore._field_indices(lines, bounds, "SubArrayId")
                    if len(matches) != 1:
                        raise ValueError("Subarray must have one SubArrayId")
                    identifier = FIELD.fullmatch(lines[matches[0]].rstrip("\r\n"))[2]
                    if not identifier.isdigit() or int(identifier) in arrays:
                        raise ValueError("SubArrayId must be a unique positive integer")
                    if int(identifier) < 1:
                        raise ValueError("SubArrayId must be a unique positive integer")
                    arrays[int(identifier)] = bounds
                active = None
        if active is not None or not arrays:
            raise ValueError("Unclosed section or missing subarray")
        return arrays, system

    @staticmethod
    def _main_array(lines: list[str]) -> tuple[int, int] | None:
        starts = [i for i, line in enumerate(lines) if line.strip() == "PVObject_PVMainArray=pvMainArray"]
        ends = [i for i, line in enumerate(lines) if line.strip() == "End of PVObject pvMainArray"]
        if not starts and not ends:
            return None
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise ValueError("Variant must have one complete main array")
        return starts[0], ends[0]

    @staticmethod
    def _field_indices(lines: list[str], bounds: tuple[int, int], key: str) -> list[int]:
        if lines[bounds[0]].strip() in (*STARTS.values(), "PVObject_PVMainArray=pvMainArray"):
            root = next(block for block in objects("".join(lines[bounds[0]:bounds[1] + 1])) if not block.path)
            return [bounds[0] + index for index, _ in root.fields.get(key, [])]
        return [index for index in range(bounds[0] + 1, bounds[1])
                if (match := FIELD.fullmatch(lines[index].rstrip("\r\n"))) and match[1] == key]

    @staticmethod
    def _display_name_fields(lines: list[str], complete: bool = False) -> list[int]:
        child = next((i for i in range(1, len(lines)) if lines[i].strip().startswith("PVObject_")
                      or re.match(r"\s*PrintSettings\s*=", lines[i])), len(lines) - 1)
        indices = VariantStore._field_indices(lines, (0, child), "Comment")
        if complete:
            # Normalize the orientation alias only in the parser view, never saved bytes.
            view = re.sub(r"(?m)^([ \t]*)End of TOrientGroup([ \t]*)(?=\r?$)",
                          r"\1End of PVObject pvOrient\2", "".join(lines))
            root = next(block for block in objects(view) if not block.path)
            if len(indices) != 1 or indices != [i for i, _ in root.fields.get("Comment", [])]:
                raise ValueError("Display name requires one owning header Comment")
        return indices

    @staticmethod
    def _validate_display_name(value: str, field: str) -> None:
        if (not isinstance(value, str) or not value or value != value.strip()
                or not value.isprintable() or len(value.encode("utf-8")) > 80):
            raise ValueError(f"Invalid {field}: require 1..80 UTF-8 bytes of printable text without surrounding whitespace")

    @staticmethod
    def _display_name_prefix(data: bytes, name: str, add_utf8_bom: bool, field: str) -> bytes:
        bom = data.startswith(b"\xef\xbb\xbf")
        if not name.isascii() and not bom and not add_utf8_bom:
            raise ValueError(f"Non-ASCII {field} values require UTF-8 BOM; use add_utf8_bom=true explicitly")
        return b"\xef\xbb\xbf" if bom or add_utf8_bom else b""

    @staticmethod
    def _check_hash(data: bytes, expected: str) -> None:
        if (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected)
                or hashlib.sha256(data).hexdigest() != expected.lower()):
            raise ValueError("Variant changed since inspection; provide current sha256")

    def inspect(self, project: str, variant: str) -> dict:
        with self.lock:
            self._check_project(project)
            data = self._read(self._path(project, variant))
            _, lines = self._text(data)
            arrays, system = self._sections(lines)
            def values(bounds, types):
                return {kind: [FIELD.fullmatch(lines[i].rstrip("\r\n"))[2]
                               for i in self._field_indices(lines, bounds, FIELDS[kind])]
                        for kind in types}
            return {"project": project, "variant": variant.upper(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "subarrays": [{"id": identifier, "references": values(bounds, ("PAN", "OND"))}
                                  for identifier, bounds in sorted(arrays.items())],
                    "system_references": values(system, ("BTR", "GEN")) if system else {}}

    def _replace(self, data: bytes, updates: dict[str, str], subarray_id: int) -> bytes:
        if (not isinstance(updates, dict) or not updates
                or any(kind not in FIELDS for kind in updates)):
            raise ValueError("Choose one or more of PAN, OND, BTR, GEN")
        if not isinstance(subarray_id, int) or isinstance(subarray_id, bool) or subarray_id < 1:
            raise ValueError("subarray_id must be a positive integer")
        _, lines = self._text(data)
        arrays, system = self._sections(lines)
        for kind, filename in updates.items():
            component = self.components._path(kind, filename)
            if not component.is_file() and self.components.builtin:
                component = self.components._path(kind, filename, "builtin")
            if not component.is_file():
                raise FileNotFoundError(f"Component {filename} must exist as a loose file")
            inspected = self.components._inspect(kind, self.components._read(component))
            self.components._require_valid(inspected, complete=inspected["format"] == "text")
            bounds = arrays.get(subarray_id) if kind in ("PAN", "OND") else system
            if bounds is None:
                raise ValueError(f"No section for {kind} at subarray {subarray_id}")
            indices = self._field_indices(lines, bounds, FIELDS[kind])
            if len(indices) != 1:
                raise ValueError(f"{FIELDS[kind]} must occur exactly once in its section")
            index = indices[0]
            line = lines[index]
            prefix = line[:line.index("=") + 1]
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines[index] = f"{prefix}{filename}{ending}"
        candidate = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")
        self._text(candidate)
        return candidate

    @staticmethod
    def _orientation(lines: list[str], number: int) -> tuple[int, int]:
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError("orientation_id must be a positive integer")
        groups = []
        start = None
        for index, line in enumerate(lines):
            value = line.strip()
            if value == "PVObject_=pvOrient":
                if start is not None:
                    raise ValueError("Nested orientations")
                start = index
            elif value == "End of TOrientGroup" and start is not None:
                groups.append((start, index))
                start = None
        if start is not None:
            raise ValueError("Unclosed orientation")
        matches = [bounds for bounds in groups if any(
            (match := FIELD.fullmatch(lines[i].rstrip("\r\n")))
            and match[1] == "NoOrient" and match[2] == str(number)
            for i in range(bounds[0] + 1, bounds[1]))]
        if len(matches) != 1:
            raise ValueError("Orientation ID must occur exactly once")
        return matches[0]

    def _orientation_list(self, lines):
        headers = [(i, re.fullmatch(r"Orientations, list of=([0-9]+) TOrientGroup", line.strip()))
                   for i, line in enumerate(lines) if line.strip().startswith("Orientations, list of=")]
        ends = [i for i, line in enumerate(lines) if line.strip() == "End of Orientations"]
        if len(headers) != 1 or not headers[0][1] or len(ends) != 1 or headers[0][0] >= ends[0]:
            raise ValueError("Expected one counted TOrientGroup orientation list")
        header, match = headers[0]
        starts = [i for i in range(header + 1, ends[0]) if lines[i].strip() == "PVObject_=pvOrient"]
        closings = [i for i in range(header + 1, ends[0]) if lines[i].strip() == "End of TOrientGroup"]
        if (len(starts) != sum(line.strip() == "PVObject_=pvOrient" for line in lines)
                or any(line.strip() == "PVObject_=pvDomeOrient" for line in lines)):
            raise ValueError("Only listed TOrientGroup orientation records are supported")
        if not starts or len(starts) != len(closings) or int(match[1]) != len(starts):
            raise ValueError("Orientation list count differs from its records")
        groups = []
        for position, (first, last) in enumerate(zip(starts, closings)):
            if first >= last or position and closings[position - 1] >= first:
                raise ValueError("Overlapping orientation records")
            ids = self._field_indices(lines, (first, last), "NoOrient")
            if len(ids) != 1:
                raise ValueError("Orientation requires one ID")
            raw = FIELD.fullmatch(lines[ids[0]].rstrip("\r\n"))[2]
            if not raw.isdigit() or not 1 <= int(raw) <= 2147483647:
                raise ValueError("Orientation ID must be a positive 32-bit integer")
            if self._orientation(lines, int(raw)) != (first, last):
                raise ValueError("Orientation is outside its declared list")
            groups.append((first, last, int(raw)))
        arrays, _ = self._sections(lines)
        identifiers = {identifier for _, _, identifier in groups}
        referenced = set()
        for first, last in arrays.values():
            root = next(block for block in objects("".join(lines[first:last + 1])) if not block.path)
            raw = root.one("NoOrientation")[1]
            if not raw.isdigit() or int(raw) not in identifiers:
                raise ValueError("Subarray references an orientation outside its declared list")
            referenced.add(int(raw))
        if unused := identifiers - referenced:
            raise ValueError(f"Orientation has no assigned subarray: {sorted(unused)}")
        return header, groups

    @staticmethod
    def _ordinary_fixed_plane(lines, bounds, allow_missing_flags=False):
        if any(line.strip().startswith("PVObject_") or line.strip().endswith("=Start")
               for line in lines[bounds[0] + 1:bounds[1]]):
            raise ValueError("Operation requires a flat fixed-plane record")
        for key, expected in (("FieldType", "FixedPlane"), ("Flags", "$00")):
            indices = VariantStore._field_indices(lines, bounds, key)
            if key == "Flags" and not indices and allow_missing_flags:
                continue
            if len(indices) != 1 or FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2] != expected:
                raise ValueError(f"Operation requires ordinary {key}={expected}")
        for key in ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver"):
            indices = VariantStore._field_indices(lines, bounds, key)
            if indices and (len(indices) != 1 or FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2] != "False"):
                raise ValueError("Averaged orientations require separate geometry handling")

    @staticmethod
    def _require_no_geometry(lines, require_shading_section=False):
        if any("PVObject_ShdTable" in line or "ListeObjets, list of=" in line
               or "PVObject_ModuleLayout" in line for line in lines):
            raise ValueError("Operation has an active shading scene, dependent shading table or module layout")
        shade = [i for i, line in enumerate(lines) if line.strip() == "PVObject_Ombrage=pvShading"]
        if not shade and not require_shading_section:
            return
        if len(shade) != 1:
            raise ValueError("Expected one empty shading section")
        end = next((i for i in range(shade[0] + 1, len(lines))
                    if lines[i].strip() == "End of PVObject pvShading"), None)
        if end is None or any(line.strip().startswith("PVObject_") for line in lines[shade[0] + 1:end]):
            raise ValueError("Operation has an active shading scene")
        flags = VariantStore._field_indices(lines, (shade[0], end), "Flags")
        if len(flags) != 1 or FIELD.fullmatch(lines[flags[0]].rstrip("\r\n"))[2] not in EMPTY_SHADING_FLAGS:
            raise ValueError("Shading is active or unrecognized: active shading scene")

    def inspect_parameters(self, project: str, variant: str) -> dict:
        with self.lock:
            self._check_project(project)
            data = self._read(self._path(project, variant))
            _, lines = self._text(data)
            arrays, system = self._sections(lines)
            def fields(bounds, names):
                return {name: values[0] for name in names
                        if len(values := [FIELD.fullmatch(lines[i].rstrip("\r\n"))[2]
                                         for i in self._field_indices(lines, bounds, name)]) == 1}
            orientations = []
            for index, line in enumerate(lines):
                if line.strip() != "PVObject_=pvOrient":
                    continue
                end = next((i for i in range(index + 1, len(lines))
                            if lines[i].strip() == "End of TOrientGroup"), None)
                if end is None:
                    raise ValueError("Unclosed orientation")
                indices = self._field_indices(lines, (index, end), "NoOrient")
                if len(indices) != 1:
                    raise ValueError("Orientation requires one ID")
                number = FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]
                if not number.isdigit():
                    raise ValueError("Orientation ID must be numeric")
                self._orientation(lines, int(number))
                orientations.append({"id": int(number), "values": fields((index, end),
                                     ("FieldType", "FieldTilt", "FieldAzim", "SummerTilt", "WinterTilt",
                                      "WinterMonths", "UsesTiltAver"))})
            main_array = self._main_array(lines)
            names = self._display_name_fields(lines)
            return {"project": project, "variant": variant.upper(),
                    "variant_name": FIELD.fullmatch(lines[names[0]].rstrip("\r\n"))[2] if len(names) == 1 else None,
                    "utf8_bom": data.startswith(b"\xef\xbb\xbf"),
                    "main_array": {"values": fields(main_array, ("UFactor", "Flags", "Flags2")) if main_array is not None else {},
                                   "soiling": soiling_state(lines, main_array) if main_array is not None else None},
                    "sha256": hashlib.sha256(data).hexdigest(), "orientations": orientations,
                    "system_parameters": fields(system, ("SystemType", "Flags", "TempShift", "TempFixed",
                                                         "TanPhi", "PLimGrid", "LimGrSeparLoss")) if system is not None else {},
                    "generator": generator_state(lines, arrays, system),
                    "grid_limit": grid_limit_state(lines, system),
                    "auxiliaries": auxiliary_state(lines, system),
                    "horizon": horizon_state(lines),
                    "power_factor": power_factor_state(lines, system),
                    "p50_p90": p50_state(lines),
                    "power_controls": power_controls_state(lines, system),
                    "ageing": ageing_model_state(lines, main_array) if main_array is not None else None,
                    "inverter_temperature": inverter_temperature_state(lines, system),
                    "subarrays": [{"id": identifier, "values": fields(bounds, (
                        "NModSerie", "NStringCh", "NStrOrient1", "NInverter", "NInvMPPT", "NoOrientation",
                        "VBkUpEncl_syst", "VBkUpDecl_syst", "LIDLoss", "PMismMPP", "PQualityModU", "PQualityModI", "PQualityModP", "Flags")),
                        "string_mismatch": string_mismatch_state(lines, bounds),
                        "iam": iam_model_state(lines, bounds)}
                                  for identifier, bounds in sorted(arrays.items())]}

    def update_parameters(self, project: str, variant: str, expected_sha256: str,
                          subarray_id: int, orientation_id: int,
                          subarray_updates: dict[str, float] | None = None,
                          orientation_updates: dict[str, float] | None = None,
                          generator_updates: dict | None = None,
                          dry_run: bool = False, power_sharing_enabled: bool | None = None,
                          independent_mppt_enabled: bool | None = None,
                          main_array_updates: dict[str, float] | None = None,
                          variant_name: str | None = None, add_utf8_bom: bool = False,
                          soiling_updates: dict | None = None,
                          grid_limit_updates: dict | None = None,
                          auxiliary_updates: dict | None = None,
                          seasonal_updates: dict | None = None,
                          horizon_updates: dict | None = None,
                          inverter_temperature_updates: dict | None = None,
                          power_factor_updates: dict | None = None,
                          p50_p90_updates: dict | None = None,
                          power_controls_updates: dict | None = None,
                          string_mismatch_updates: dict | None = None,
                          ageing_updates: dict | None = None,
                          iam_updates: dict | None = None) -> dict:
        allowed_subarray = {"NModSerie": (1, 100), "NInverter": (1, 10000), "NStringCh": (1, 2147483647),
                            "NoOrientation": (1, 2147483647), "NInvMPPT": (1, 10000),
                            "VBkUpEncl_syst": (0, 1),
                            "VBkUpDecl_syst": (0, 1), "LIDLoss": (0, 100), "PMismMPP": (0, 100),
                             "ModuleQualityLoss": (-5, 20)}
        allowed_orientation = {"FieldTilt": (0, 90), "FieldAzim": (-180, 180)}
        allowed_main_array = {"UFactor": (15, 100)}
        requested = ((subarray_updates, allowed_subarray), (orientation_updates, allowed_orientation),
                     (main_array_updates, allowed_main_array))
        if soiling_updates is not None and (any(changes for changes, _ in requested)
                or generator_updates is not None or power_sharing_enabled is not None or independent_mppt_enabled is not None):
            raise ValueError("Soiling updates require a separate transaction; variant_name may accompany them")
        if grid_limit_updates is not None and (any(changes for changes, _ in requested)
                or generator_updates is not None or soiling_updates is not None
                or power_sharing_enabled is not None or independent_mppt_enabled is not None):
            raise ValueError("Grid-limit updates require a separate transaction; variant_name may accompany them")
        if auxiliary_updates is not None and (any(changes for changes, _ in requested)
                or generator_updates is not None or soiling_updates is not None or grid_limit_updates is not None
                or power_sharing_enabled is not None or independent_mppt_enabled is not None):
            raise ValueError("Auxiliary updates require a separate transaction; variant_name may accompany them")
        if seasonal_updates is not None and (any(changes for changes, _ in requested)
                or generator_updates is not None or soiling_updates is not None or grid_limit_updates is not None
                or auxiliary_updates is not None or power_sharing_enabled is not None or independent_mppt_enabled is not None):
            raise ValueError("Seasonal updates require a separate transaction; variant_name may accompany them")
        if horizon_updates is not None and (any(changes for changes, _ in requested)
                or generator_updates is not None or soiling_updates is not None or grid_limit_updates is not None
                or auxiliary_updates is not None or seasonal_updates is not None
                or power_sharing_enabled is not None or independent_mppt_enabled is not None):
            raise ValueError("Horizon updates require a separate transaction; variant_name may accompany them")
        if inverter_temperature_updates is not None and (any(changes for changes, _ in requested)
                or generator_updates is not None or soiling_updates is not None or grid_limit_updates is not None
                or auxiliary_updates is not None or seasonal_updates is not None or horizon_updates is not None
                or power_sharing_enabled is not None or independent_mppt_enabled is not None):
            raise ValueError("Inverter-temperature updates require a separate transaction; variant_name may accompany them")
        if power_factor_updates is not None and (any(changes for changes, _ in requested)
                or generator_updates is not None or soiling_updates is not None or grid_limit_updates is not None
                or auxiliary_updates is not None or seasonal_updates is not None or horizon_updates is not None
                or inverter_temperature_updates is not None or power_sharing_enabled is not None
                or independent_mppt_enabled is not None):
            raise ValueError("Power-factor updates require a separate transaction; variant_name may accompany them")
        if p50_p90_updates is not None and (any(changes for changes, _ in requested)
                or generator_updates is not None or soiling_updates is not None or grid_limit_updates is not None
                or auxiliary_updates is not None or seasonal_updates is not None or horizon_updates is not None
                or inverter_temperature_updates is not None or power_factor_updates is not None
                or power_sharing_enabled is not None or independent_mppt_enabled is not None):
            raise ValueError("P50/P90 updates require a separate transaction; variant_name may accompany them")
        if power_controls_updates is not None and (any(changes for changes, _ in requested)
                or generator_updates is not None or soiling_updates is not None or grid_limit_updates is not None
                or auxiliary_updates is not None or seasonal_updates is not None or horizon_updates is not None
                or inverter_temperature_updates is not None or power_factor_updates is not None
                or p50_p90_updates is not None or string_mismatch_updates is not None
                or power_sharing_enabled is not None or independent_mppt_enabled is not None):
            raise ValueError("Combined power controls require a separate transaction; variant_name may accompany them")
        if string_mismatch_updates is not None and (any(changes for changes, _ in requested)
                or generator_updates is not None or soiling_updates is not None or grid_limit_updates is not None
                or auxiliary_updates is not None or seasonal_updates is not None or horizon_updates is not None
                or inverter_temperature_updates is not None or power_factor_updates is not None
                or p50_p90_updates is not None or power_controls_updates is not None
                or power_sharing_enabled is not None or independent_mppt_enabled is not None):
            raise ValueError("String-mismatch updates require a separate transaction; variant_name may accompany them")
        for extra, label in ((ageing_updates, "Ageing model"), (iam_updates, "IAM model")):
            if extra is not None and (any(changes for changes, _ in requested)
                    or generator_updates is not None or soiling_updates is not None or grid_limit_updates is not None
                    or auxiliary_updates is not None or seasonal_updates is not None or horizon_updates is not None
                    or inverter_temperature_updates is not None or power_factor_updates is not None
                    or p50_p90_updates is not None or power_controls_updates is not None
                    or string_mismatch_updates is not None or ageing_updates is not None and iam_updates is not None
                    or power_sharing_enabled is not None or independent_mppt_enabled is not None):
                raise ValueError(f"{label} updates require a separate transaction; variant_name may accompany them")
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        if not isinstance(add_utf8_bom, bool):
            raise ValueError("add_utf8_bom must be a boolean")
        if add_utf8_bom and variant_name is None:
            raise ValueError("add_utf8_bom requires variant_name")
        if variant_name is not None:
            self._validate_display_name(variant_name, "variant_name")
        for name, switch in (("power_sharing_enabled", power_sharing_enabled), ("independent_mppt_enabled", independent_mppt_enabled)):
            if switch is not None:
                if not isinstance(switch, bool):
                    raise ValueError(f"{name} must be a boolean")
                if (any(changes for changes, _ in requested) or generator_updates is not None or variant_name is not None
                        or power_sharing_enabled is not None and independent_mppt_enabled is not None):
                    raise ValueError("Mode/sharing changes require separate parameter transactions")
        if (not any(changes for changes, _ in requested) and not generator_updates and variant_name is None
                and power_sharing_enabled is None and independent_mppt_enabled is None
                and soiling_updates is None and grid_limit_updates is None and auxiliary_updates is None
                and seasonal_updates is None and horizon_updates is None and inverter_temperature_updates is None
                and power_factor_updates is None and p50_p90_updates is None and power_controls_updates is None
                and string_mismatch_updates is None
                and ageing_updates is None and iam_updates is None) or any(
                changes is not None and (not isinstance(changes, dict) or
                                         any(key not in allowed for key in changes))
                for changes, allowed in requested):
            raise ValueError("Provide supported parameter updates or variant_name")
        if generator_updates is not None and (not isinstance(generator_updates, dict) or not generator_updates):
            raise ValueError("generator_updates must be a nonempty dictionary")
        if generator_updates is not None and set(subarray_updates or {}) & {"VBkUpEncl_syst", "VBkUpDecl_syst"}:
            raise ValueError("Use generator_updates.thresholds for thresholds in a generator transaction")
        with self.lock:
            self._check_project(project)
            path = self._path(project, variant)
            data = self._read(path)
            self._check_hash(data, expected_sha256)
            text, lines = self._text(data)
            arrays, system = self._sections(lines)
            name_change = None
            bom_prefix = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
            if variant_name is not None:
                bom_prefix = self._display_name_prefix(data, variant_name, add_utf8_bom, "variant_name")
                index, = self._display_name_fields(lines, complete=True)
                name_change = {"before": FIELD.fullmatch(lines[index].rstrip("\r\n"))[2], "after": variant_name}
                lines[index] = replace_value(lines[index], variant_name)
            main_array = self._main_array(lines) if main_array_updates else None
            if main_array_updates:
                kinds = self._field_indices(lines, system, "SystemType") if system is not None else []
                versions = [self._field_indices(lines, bounds, "Version") for bounds in arrays.values()]
                if (main_array is None or len(kinds) != 1 or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid"
                        or any(len(indices) != 1 or FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2] not in ("8.1.0", "8.1.6") for indices in versions)):
                    raise ValueError("Thermal editing requires a verified 8.1.0/8.1.6 grid main array")
                for key, expected in (("Flags", "$00100400"), ("Flags2", "$082A")):
                    indices = self._field_indices(lines, main_array, key)
                    if len(indices) != 1 or FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2] != expected:
                        raise ValueError("Thermal editing requires verified main-array Flags/Flags2")
                self._require_no_geometry(lines)
            if subarray_updates or power_sharing_enabled is not None or independent_mppt_enabled is not None:
                if not isinstance(subarray_id, int) or isinstance(subarray_id, bool) or subarray_id not in arrays:
                    raise ValueError("Subarray ID does not exist")
            for loss in set(subarray_updates or {}) & {"LIDLoss", "PMismMPP", "ModuleQualityLoss"}:
                versions = self._field_indices(lines, arrays[subarray_id], "Version")
                kinds = self._field_indices(lines, system, "SystemType") if system is not None else []
                supported = ("8.1.0", "8.1.6")
                if (len(versions) != 1 or FIELD.fullmatch(lines[versions[0]].rstrip("\r\n"))[2] not in supported
                        or len(kinds) != 1 or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid"):
                    raise ValueError(f"{loss} editing requires a verified {'/'.join(supported)} grid subarray")
            if set(subarray_updates or {}) & {"NModSerie", "LIDLoss", "PMismMPP", "ModuleQualityLoss"}:
                self._require_no_geometry(lines)
            loss_modes = {}
            for key, label, mask in (("PMismMPP", "Mismatch", 0x00040000),
                                     ("ModuleQualityLoss", "Module quality", 0x00100000)):
                if key not in (subarray_updates or {}):
                    continue
                indices = self._field_indices(lines, arrays[subarray_id], "Flags")
                if len(indices) != 1:
                    raise ValueError(f"{label} editing requires one owning Flags field")
                index = indices[0]
                raw = FIELD.fullmatch(lines[index].rstrip("\r\n"))[2]
                if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", raw):
                    raise ValueError(f"{label} editing requires hexadecimal Flags")
                flags = int(raw[1:], 16)
                loss_modes[key] = {"default_before": bool(flags & mask), "default_after": False}
                if loss_modes[key]["default_before"]:
                    lines[index] = replace_value(lines[index], f"${flags & ~mask:0{len(raw) - 1}X}")
            targets = [(subarray_updates, allowed_subarray, arrays[subarray_id])] if subarray_updates else []
            if main_array_updates:
                targets.append((main_array_updates, allowed_main_array, main_array))
            if orientation_updates:
                bounds = self._orientation(lines, orientation_id)
                if any("PVObject_ShdTable" in line for line in lines[bounds[0]:bounds[1] + 1]):
                    raise ValueError("Orientation has a dependent shading table")
                self._require_no_geometry(lines)
                self._ordinary_fixed_plane(lines, bounds, allow_missing_flags=True)
                targets.append((orientation_updates, allowed_orientation, bounds))
            for changes, allowed, bounds in targets:
                for name, value in changes.items():
                    if (not isinstance(value, (int, float)) or isinstance(value, bool)
                            or not math.isfinite(value) or not allowed[name][0] <= value <= allowed[name][1]
                            or name in ("NModSerie", "NInverter", "NStringCh", "NInvMPPT", "NoOrientation") and int(value) != value):
                        raise ValueError(f"Invalid {name} value")
                    if name == "ModuleQualityLoss":
                        for field in ("PQualityModU", "PQualityModI"):
                            indices = self._field_indices(lines, bounds, field)
                            if len(indices) != 1:
                                raise ValueError(f"{field} must occur exactly once in its section")
                            lines[indices[0]] = replace_value(lines[indices[0]], f"{value:.3f}")
                        continue
                    indices = self._field_indices(lines, bounds, name)
                    if len(indices) != 1:
                        raise ValueError(f"{name} must occur exactly once in its section")
                    if name in ("NInverter", "NStringCh", "NInvMPPT", "NoOrientation"):
                        continue  # Grid quantities and orientation references are checked together below.
                    index = indices[0]
                    old = lines[index]
                    prefix = old[:old.index("=") + 1]
                    ending = "\r\n" if old.endswith("\r\n") else "\n" if old.endswith("\n") else ""
                    rendered = str(int(value)) if name == "NModSerie" else f"{value:.3f}"
                    lines[index] = f"{prefix}{rendered}{ending}"
            capacity = None
            assignment = None
            sharing = None
            mppt_mode = None
            if independent_mppt_enabled is not None or power_sharing_enabled is not None or subarray_updates and set(subarray_updates) & {"NInverter", "NStringCh", "NInvMPPT", "NoOrientation"}:
                kinds = self._field_indices(lines, system, "SystemType") if system is not None else []
                if (len(kinds) != 1
                        or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid"):
                    raise ValueError("Inverter capacity editing requires a grid system")
                self._require_no_geometry(lines, require_shading_section=True)
                fields = self._field_indices(lines, arrays[subarray_id], "NoOrientation")
                if len(fields) != 1:
                    raise ValueError("Capacity editing requires one fixed-plane orientation")
                orientation_raw = FIELD.fullmatch(lines[fields[0]].rstrip("\r\n"))[2]
                if not orientation_raw.isdigit():
                    raise ValueError("Capacity orientation ID must be an integer")
                orientation = self._orientation(lines, int(orientation_raw))
                types = self._field_indices(lines, orientation, "FieldType")
                if len(types) != 1 or FIELD.fullmatch(lines[types[0]].rstrip("\r\n"))[2] != "FixedPlane":
                    raise ValueError("Capacity editing requires a fixed-plane orientation")
                if independent_mppt_enabled is not None:
                    mppt_mode = set_independent_mppt(lines, arrays, subarray_id, independent_mppt_enabled)
                if power_sharing_enabled is not None:
                    sharing = set_power_sharing(lines, arrays, subarray_id, power_sharing_enabled)
                if set(subarray_updates or {}) & {"NInverter", "NStringCh", "NInvMPPT"}:
                    capacity = resize_capacity(lines, arrays, subarray_id,
                        **{key: int(subarray_updates[field]) for key, field in (
                            ("target_inverters", "NInverter"), ("target_strings", "NStringCh"), ("target_mppt", "NInvMPPT"))
                           if field in subarray_updates})
                if "NoOrientation" in (subarray_updates or {}):
                    if grid_inventory(lines, arrays)["counts_valid"] is not True:
                        raise ValueError("Orientation assignment requires complete consistent grid counts")
                    _, groups = self._orientation_list(lines)
                    if [identifier for _, _, identifier in groups] != list(range(1, len(groups) + 1)):
                        raise ValueError("Orientation assignment requires sequential ordered IDs")
                    branches, root = self._subarray_branches(lines, arrays, subarray_id)
                    self._require_plane_branch_mode(root, branches)
                    if (root.one("NStrOrient1")[1] != root.one("NStringCh")[1] or any(
                            key.startswith("NStrOrient") and key != "NStrOrient1" and root.one(key)[1] != "0"
                            for key in root.fields)):
                        raise ValueError("Orientation assignment requires all strings in one orientation")
                    destination = int(subarray_updates["NoOrientation"])
                    self._ordinary_fixed_plane(lines, orientation)
                    self._ordinary_fixed_plane(lines, self._orientation(lines, destination))
                    if int(orientation_raw) != destination:
                        index = arrays[subarray_id][0] + root.one("NoOrientation")[0]
                        lines[index] = replace_value(lines[index], str(destination))
                    self._orientation_list(lines)
                    assignment = {"subarray_id": subarray_id, "before": int(orientation_raw), "after": destination}
            generator = None
            if generator_updates is not None:
                generator = replace_generator(lines, arrays, system, generator_updates, self.components)
            soiling_change = grid_limit_change = auxiliary_change = seasonal_change = horizon_change = temperature_change = None
            power_factor_change = None
            p50_change = None
            power_controls_change = None
            string_mismatch_change = None
            ageing_change = None
            iam_change = None
            if any(value is not None for value in (soiling_updates, grid_limit_updates, auxiliary_updates, seasonal_updates, horizon_updates, inverter_temperature_updates, power_factor_updates, p50_p90_updates, power_controls_updates, string_mismatch_updates, ageing_updates, iam_updates)):
                control = ("Soiling" if soiling_updates is not None else "Grid limit" if grid_limit_updates is not None
                           else "Auxiliary" if auxiliary_updates is not None else "Seasonal" if seasonal_updates is not None
                           else "Horizon" if horizon_updates is not None else "Inverter temperature" if inverter_temperature_updates is not None
                           else "Power factor" if power_factor_updates is not None else "P50/P90" if p50_p90_updates is not None
                           else "Ageing model" if ageing_updates is not None
                           else "IAM model" if iam_updates is not None
                           else "String mismatch" if string_mismatch_updates is not None
                           else "Combined power controls")
                main = self._main_array(lines)
                versions = [self._field_indices(lines, b, "Version") for b in arrays.values()]
                header_end = next(i for i in range(1, len(lines)) if lines[i].strip().startswith("PVObject_"))
                root_versions = self._field_indices(lines, (0, header_end), "Version")
                kinds = self._field_indices(lines, system, "SystemType") if system is not None else []
                if (len(root_versions) != 1 or FIELD.fullmatch(lines[root_versions[0]].rstrip("\r\n"))[2] != "8.1.6"
                        or main is None or len(arrays) != 1 or len(kinds) != 1
                        or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid"
                        or any(len(v) != 1 or FIELD.fullmatch(lines[v[0]].rstrip("\r\n"))[2] != "8.1.6" for v in versions)
                        or any("pvGridStorage" in line for line in lines)):
                    raise ValueError(f"{control} editing requires a native 8.1.6 PV-only single-subarray Grid variant")
                for key, accepted in (("Flags", ("$00100400", "$01900400")),
                                      ("Flags2", ("$0010082A",) if ageing_updates is not None else ("$082A",))):
                    indices = self._field_indices(lines, main, key)
                    if len(indices) != 1 or FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2] not in accepted:
                        raise ValueError(f"{control} editing requires verified main-array Flags/Flags2; flags stay unchanged")
                self._require_no_geometry(lines)
                if sum(line.strip() == "PVObject_=pvOrient" for line in lines) != 1:
                    raise ValueError(f"{control} editing currently verifies one fixed plane")
                if seasonal_updates is None:
                    self._ordinary_fixed_plane(lines, self._orientation(lines, 1))
                if soiling_updates is not None:
                    soiling_change = replace_soiling(lines, main, soiling_updates)
                elif grid_limit_updates is not None:
                    grid_limit_change = replace_grid_limit(lines, system, grid_limit_updates)
                elif auxiliary_updates is not None and auxiliary_updates.get("mode") != "profile":
                    auxiliary_change = replace_auxiliaries(lines, system, auxiliary_updates)
                else:
                    if (orientation_id != 1 or [g[2] for g in self._orientation_list(lines)[1]] != [1]
                            or any("pvLoad" in line for line in lines)
                            or (seasonal_updates is not None or inverter_temperature_updates is not None or power_factor_updates is not None or p50_p90_updates is not None or power_controls_updates is not None
                                or string_mismatch_updates is not None
                                or ageing_updates is not None or iam_updates is not None
                                or (auxiliary_updates is not None and auxiliary_updates.get("mode") == "profile"))
                            and any("PVObject_Horizon=" in line for line in lines)):
                        raise ValueError(f"{control} editing requires one orientation, no loads and a supported horizon state")
                    if (inverter_temperature_updates is None and power_factor_updates is None
                            and power_controls_updates is None
                            and not (auxiliary_updates is not None and auxiliary_updates.get("mode") == "profile")):
                        flags = self._field_indices(lines, system, "Flags")
                        if len(flags) != 1 or not re.fullmatch(r"\$0{1,8}", FIELD.fullmatch(lines[flags[0]].rstrip("\r\n"))[2]):
                            raise ValueError(f"{control} editing requires plain Grid system Flags=$00")
                    bounds = next(iter(arrays.values()))
                    root = next(block for block in objects("".join(lines[bounds[0]:bounds[1] + 1])) if not block.path)
                    strings = root.one("NStringCh")[1]
                    if (root.one("NoOrientation")[1] != "1" or not strings.isdigit() or int(strings) < 1
                            or root.one("NStrOrient1")[1] != strings
                            or any(key.startswith("NStrOrient") and key != "NStrOrient1" and root.one(key)[1] != "0"
                                   for key in root.fields) or grid_inventory(lines, arrays)["counts_valid"] is not True):
                        raise ValueError(f"{control} editing requires complete circuit counts and every string in orientation 1")
                    if ageing_updates is not None or iam_updates is not None:
                        sflags = self._field_indices(lines, bounds, "Flags")
                        if (len(sflags) != 1
                                or FIELD.fullmatch(lines[sflags[0]].rstrip("\r\n"))[2] != "$019E1002"
                                or any(re.match(r"\s*P50_P90\s*=", line) for line in lines)):
                            raise ValueError(f"{control} editing requires the verified 019E1002 subarray Flags and no P50/P90 assignment")
                    if string_mismatch_updates is not None:
                        sflags = self._field_indices(lines, bounds, "Flags")
                        if (len(sflags) != 1
                                or int(FIELD.fullmatch(lines[sflags[0]].rstrip("\r\n"))[2].lstrip("$"), 16) & ~0x00020000 != 0x019C1002
                                or any("P50_P90=TP50_P90" in line for line in lines)):
                            raise ValueError(f"{control} editing requires the verified 0x019C1002 subarray flag family and no P50/P90 block")
                    if inverter_temperature_updates is not None:
                        temperature_change = replace_inverter_temperature(lines, system, inverter_temperature_updates)
                    elif power_factor_updates is not None:
                        power_factor_change = replace_power_factor(lines, system, power_factor_updates)
                    elif p50_p90_updates is not None:
                        p50_change = replace_p50(lines, p50_p90_updates)
                    elif power_controls_updates is not None:
                        power_controls_change = replace_power_controls(lines, system, power_controls_updates)
                    elif auxiliary_updates is not None:
                        auxiliary_change = replace_auxiliaries(lines, system, auxiliary_updates)
                    elif string_mismatch_updates is not None:
                        string_mismatch_change = replace_string_mismatch(lines, bounds, string_mismatch_updates)
                    elif ageing_updates is not None:
                        ageing_change = replace_ageing_model(lines, main, ageing_updates)
                    elif iam_updates is not None:
                        iam_change = replace_iam_model(lines, bounds, iam_updates)
                    else:
                        updates = seasonal_updates if seasonal_updates is not None else horizon_updates
                        name_key = "orientation_name" if seasonal_updates is not None else "profile_name"
                        display_name = updates.get(name_key) if isinstance(updates, dict) else None
                        if display_name is not None:
                            self._validate_display_name(display_name, name_key)
                            if not display_name.isascii() and not bom_prefix:
                                raise ValueError(f"Non-ASCII {name_key} requires an explicit UTF-8 BOM")
                        if seasonal_updates is not None:
                            seasonal_change = replace_seasonal(lines, self._orientation(lines, 1), seasonal_updates)
                        else:
                            horizon_change = replace_horizon(lines, horizon_updates)
            edited_text = "".join(lines)
            candidate = bom_prefix + edited_text.encode("utf-8")
            self._text(candidate)
            diff = "".join(itertools.islice(difflib.unified_diff(
                text.splitlines(keepends=True), lines, fromfile=path.name,
                tofile=path.name + " (candidate)"), 501))
            result = {"project": project, "variant": variant.upper(),
                "before_sha256": hashlib.sha256(data).hexdigest(),
                "sha256": hashlib.sha256(candidate).hexdigest(), "changed": candidate != data,
                "dry_run": dry_run, "diff": diff[:32000],
                "diff_truncated": len(diff) > 32000 or len(diff.splitlines()) > 500}
            if soiling_change is not None:
                result["soiling"] = soiling_change
            if grid_limit_change is not None:
                result["grid_limit"] = grid_limit_change
            if auxiliary_change is not None:
                result["auxiliaries"] = auxiliary_change
            if seasonal_change is not None:
                result["seasonal_orientation"] = seasonal_change
            if horizon_change is not None:
                result["horizon"] = horizon_change
            if temperature_change is not None:
                result["inverter_temperature"] = temperature_change
            if power_factor_change is not None:
                result["power_factor"] = power_factor_change
            if p50_change is not None:
                result["p50_p90"] = p50_change
            if power_controls_change is not None:
                result["power_controls"] = power_controls_change
            if string_mismatch_change is not None:
                result["string_mismatch"] = string_mismatch_change
            if ageing_change is not None:
                result["ageing"] = ageing_change
            if iam_change is not None:
                result["iam"] = iam_change
            if name_change is not None:
                result.update(name_change=name_change, bom_added=bool(bom_prefix) and not data.startswith(b"\xef\xbb\xbf"))
            if "PMismMPP" in loss_modes:
                result["mismatch"] = loss_modes["PMismMPP"]
            if "ModuleQualityLoss" in loss_modes:
                result["module_quality"] = loss_modes["ModuleQualityLoss"]
            if mppt_mode is not None:
                result["mppt_mode"] = mppt_mode
            if sharing is not None:
                result["power_sharing"] = sharing
            if capacity is not None:
                result["capacity"] = capacity
            if assignment is not None:
                result["orientation_assignment"] = assignment
            if generator is not None:
                new_arrays, new_system = self._sections(lines)
                result.update(generator_before=generator["before"],
                    generator_after=generator_state(lines, new_arrays, new_system),
                    generator_component=generator["component"], warnings=generator["warnings"])
            if dry_run or candidate == data:
                return result
            if self._read(path) != data:
                raise ValueError("Variant changed while preparing the edit")
            backup = self._backup(path, data)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(candidate)
                if self._read(path) != data:
                    raise ValueError("Variant changed while preparing the edit")
                if generator is not None and generator["component"] is not None:
                    component = generator["component"]
                    if (component["library"] == "builtin" and
                            self.components._path("GEN", component["filename"]).exists()):
                        raise ValueError("Generator component changed: workspace now shadows builtin")
                    target = self.components._path("GEN", component["filename"], component["library"])
                    if hashlib.sha256(self.components._read(target)).hexdigest() != component["sha256"]:
                        raise ValueError("Generator component changed while preparing the edit")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {**result, "backup_name": backup}

    def _backup_folder(self) -> Path:
        root = self._root()
        folder = root / ".mcp-variant-backups"
        if folder.is_symlink() or folder.resolve().parent != root:
            raise ValueError("Variant backup folder resolves outside workspace")
        folder.mkdir(exist_ok=True)
        return folder

    def _backup(self, path: Path, data: bytes, transaction_id: str | None = None) -> str:
        name = f"{path.name}.{transaction_id or uuid.uuid4().hex}.bak"
        with (self._backup_folder() / name).open("xb") as stream:
            stream.write(data)
        return name

    def _create_project_files(self, project, snapshots):
        if len(snapshots) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("Project exceeds 128 PRJ/VC members")
        if len({path for path, _ in snapshots}) != len(snapshots):
            raise ValueError("Duplicate variant identifiers after case normalization")
        if any(self._belongs_to_project(path, project) for path in self._root().iterdir()):
            raise FileExistsError("Destination project or variant already exists")
        created = []
        try:
            for target, data in snapshots:
                with target.open("xb") as stream:
                    created.append(target)
                    stream.write(data)
        except BaseException:
            for target in reversed(created):
                target.unlink(missing_ok=True)
            raise
        return {"project": project, "variants": [path.suffix[1:] for path, _ in snapshots[1:]],
                "sha256": hashlib.sha256(snapshots[0][1]).hexdigest(),
                "files": {path.name: hashlib.sha256(data).hexdigest() for path, data in snapshots}}

    def create_project(self, project: str, project_content: str, variants: dict[str, str]) -> dict:
        """Create a complete supplied PRJ/VC set; no existing project is required."""
        if (not isinstance(project_content, str) or not isinstance(variants, dict) or not variants
                or any(not isinstance(content, str) for content in variants.values())):
            raise ValueError("Supply PRJ text and a nonempty VC identifier-to-text map")
        if len(variants) >= MAX_ARCHIVE_MEMBERS:
            raise ValueError("Project exceeds 128 PRJ/VC members")
        with self.lock:
            data = project_content.encode("utf-8")
            self._project_text(data)
            snapshots = [(self._path(project), data)]
            for identifier, content in variants.items():
                path = self._path(project, identifier)
                data = content.encode("utf-8")
                _, lines = self._text(data)
                self._sections(lines)
                snapshots.append((path, data))
            return self._create_project_files(project, snapshots)

    def clone_project(self, source_project: str, new_project: str) -> dict:
        """Copy a project and its variants without rewriting embedded site/meteo snapshots."""
        with self.lock:
            self._check_project(source_project)
            source_root = self._root()
            target_prj = self._path(new_project)
            sources = [self._path(source_project)]
            source_paths = list(source_root.iterdir())
            for path in source_paths:
                if path.name.casefold() == source_project.casefold():
                    continue
                if self._belongs_to_project(path, source_project):
                    extension = path.suffix[1:]
                    if not re.fullmatch(r"VC[A-Za-z0-9]+", extension, re.I):
                        raise ValueError(f"Unrecognized project sidecar: {path.name}")
                    sources.append(self._path(source_project, extension))
            if len(sources) == 1:
                raise ValueError("Source project has no variants")
            snapshots = []
            for source in sources:
                data = self._read(source)
                if source == sources[0]:
                    self._project_text(data)
                else:
                    _, lines = self._text(data)
                    self._sections(lines)
                target = target_prj if source == sources[0] else self._path(new_project, source.suffix[1:])
                snapshots.append((target, data))
            return {**self._create_project_files(new_project, snapshots), "source_project": source_project}

    def clone(self, project: str, source_variant: str, new_variant: str,
              updates: dict[str, str] | None = None, subarray_id: int = 1,
              source_project: str | None = None) -> dict:
        with self.lock:
            self._check_project(project)
            source_project = project if source_project is None else source_project
            self._check_project(source_project)
            source = self._path(source_project, source_variant)
            target = self._path(project, new_variant)
            data = self._read(source)
            _, lines = self._text(data)
            self._sections(lines)
            candidate = self._replace(data, updates, subarray_id) if updates else data
            if any(path.name.casefold() == target.name.casefold() for path in target.parent.iterdir()):
                raise FileExistsError(target.name)
            with target.open("xb") as stream:
                stream.write(candidate)
            return {"project": project, "variant": new_variant.upper(),
                    "source_project": source_project, "source_variant": source_variant.upper(),
                    "sha256": hashlib.sha256(candidate).hexdigest()}

    def update(self, project: str, variant: str, updates: dict[str, str],
               expected_sha256: str, subarray_id: int = 1) -> dict:
        with self.lock:
            self._check_project(project)
            path = self._path(project, variant)
            data = self._read(path)
            self._check_hash(data, expected_sha256)
            candidate = self._replace(data, updates, subarray_id)
            backup = self._backup(path, data)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(candidate)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {"project": project, "variant": variant.upper(), "backup_name": backup,
                    "sha256": hashlib.sha256(candidate).hexdigest()}

    @staticmethod
    def _subarray_branches(lines, arrays, identifier):
        start, end = arrays[identifier]
        root = next(block for block in objects("".join(lines[start:end + 1])) if not block.path)
        root.one("NoOrientation")
        if root.one("SubArrayId")[1] != str(identifier):
            raise ValueError("Subarray ID does not match its owning object")
        name = root.one("Comment")[1]
        for other_id, (first, last) in arrays.items():
            other = root if other_id == identifier else next(
                block for block in objects("".join(lines[first:last + 1])) if not block.path)
            if "PowerSharingInverterId" in other.fields and other.one("PowerSharingInverterId")[1] != "-1":
                raise ValueError("Shared inverter power is not supported for subarray editing")

        def walk(node):
            yield node
            for child in node.children:
                yield from walk(child)

        matches, owned = [], {start + root.one("SubArrayId")[0]}
        for node in (item for tree in circuit_nodes(lines) for item in walk(tree)):
            if node.kind != "InverterNode":
                continue
            members = list(walk(node))
            ids = [member.one("SubArrayId") for member in members]
            if str(identifier) not in ids:
                continue
            if any(value != str(identifier) for value in ids):
                raise ValueError("Subarray must own complete inverter branches without shared allocation")
            if any(member.one("SubArrayName") != name for member in members):
                raise ValueError("Circuit branch names do not match the subarray")
            owned.update(index for member in members for index in member.field_lines["SubArrayId"])
            matches.append((node.line - 1, node.end_line - 1, members))
        occurrences = {i for i, line in enumerate(lines)
                       if (field := FIELD.fullmatch(line.rstrip("\r\n")))
                       and field[1] == "SubArrayId" and field[2] == str(identifier)}
        if not matches or not occurrences.issubset(owned):
            raise ValueError("Subarray ID is referenced outside its complete inverter branches")
        checked = grid_inventory(lines, arrays)
        if any(row["id"] == identifier and row["explicit_mppt_inputs_match_declared"] is False
               for row in checked["subarrays"]):
            raise ValueError("Independent MPPT layout does not match its declared input count")
        if len(matches) > 1 and checked["counts_valid"] is not True:
            raise ValueError("Grouped subarray editing requires fully checked consistent grid counts")
        return matches, root

    @staticmethod
    def _require_plane_branch_mode(root, branches):
        flags = root.one("Flags")[1] if "Flags" in root.fields else ""
        if re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) and int(flags[1:], 16) & 0x400:
            if ("Version" not in root.fields or root.one("Version")[1] not in ("8.1.0", "8.1.6")
                    or "PowerSharingInverterId" not in root.fields or root.one("PowerSharingInverterId")[1] != "-1"
                    or any(not members[0].children or any(child.kind != "MPPTNode" for child in members[0].children)
                           for _, _, members in branches)):
                raise ValueError("Plane editing requires verified exclusive explicit MPPT allocation")
        else:
            require_ordinary_mppt(root)
            if any(member.kind == "MPPTNode" for _, _, members in branches for member in members):
                raise ValueError("Plane editing requires ordinary branches without explicit MPPT")
        if any(set(member.fields) & {"NoOrientation", "NoOrient", "OrientationId"}
               for _, _, members in branches for member in members):
            raise ValueError("Circuit-level orientation references outside owned records require separate allocation handling")

    def validate_structure(self, project: str, variant: str) -> dict:
        """Check known cross-object references; simulation remains the physical validator."""
        with self.lock:
            self._check_project(project)
            data = self._read(self._path(project, variant))
            _, lines = self._text(data)
            arrays, system = self._sections(lines)
            issues = []
            warnings = []
            orientation_ids = set()
            for index, line in enumerate(lines):
                if line.strip() not in ("PVObject_=pvOrient", "PVObject_=pvDomeOrient"):
                    continue
                closing = ("End of TOrientGroup" if line.strip() == "PVObject_=pvOrient"
                           else "End of TDomeOrientGroup")
                end = next((i for i in range(index + 1, len(lines)) if lines[i].strip() == closing), None)
                if end is None:
                    issues.append("Unclosed orientation")
                    continue
                identifiers = self._field_indices(lines, (index, end), "NoOrient")
                if len(identifiers) != 1:
                    issues.append("Orientation needs one NoOrient")
                    continue
                raw = FIELD.fullmatch(lines[identifiers[0]].rstrip("\r\n"))[2]
                if not raw.isdigit() or int(raw) in orientation_ids:
                    issues.append(f"Duplicate or invalid orientation ID: {raw}")
                else:
                    orientation_ids.add(int(raw))
            list_headers = [line.strip() for line in lines if line.strip().startswith("Orientations, list of=")]
            list_unchecked = []
            if list_headers:
                if (any(not line.endswith(" TOrientGroup") for line in list_headers)
                        or any(line.strip() == "PVObject_=pvDomeOrient" for line in lines)):
                    list_unchecked.append("orientation list counts for dome/unknown list types")
                else:
                    try:
                        self._orientation_list(lines)
                    except ValueError as exc:
                        issues.append(str(exc))
            for identifier, bounds in arrays.items():
                indices = self._field_indices(lines, bounds, "NoOrientation")
                if len(indices) != 1:
                    issues.append(f"Subarray {identifier} needs one NoOrientation")
                    continue
                raw = FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]
                if not raw.isdigit() or int(raw) not in orientation_ids:
                    issues.append(f"Subarray {identifier} references unknown orientation {raw}")
            system_type = None
            unchecked = list_unchecked
            circuit = None
            if system is None:
                issues.append("Missing pvSystem section")
                unchecked.append("system circuit references")
            else:
                kinds = self._field_indices(lines, system, "SystemType")
                if len(kinds) != 1:
                    issues.append("System needs exactly one SystemType")
                    unchecked.append("system circuit references")
                else:
                    system_type = FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2]
                    if not system_type:
                        issues.append("SystemType must not be empty")
                    if system_type != "Grid":
                        unchecked.append("system circuit references")
                        warnings.append(f"Circuit checks are not implemented for SystemType={system_type!r}")
                if system_type == "Grid":
                    circuit = grid_inventory(lines, arrays)
                    issues.extend(circuit["issues"])
                    unchecked.extend(circuit["unchecked_checks"])
            if any("PVObject_ShdTable" in line or "ListeObjets, list of=" in line for line in lines):
                warnings.append("Shading geometry and derived factor tables require simulation verification")
            return {"project": project, "variant": variant.upper(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "valid": False if issues else None if unchecked else True,
                    "checks_complete": not unchecked, "unchecked_checks": unchecked,
                    "system_type": system_type,
                    "scope": "orientation, grid circuit references and serialized inverter/string counts; not physical validation",
                    "grid_circuit": circuit,
                    "issues": issues, "warnings": warnings,
                    "subarray_ids": sorted(arrays), "orientation_ids": sorted(orientation_ids)}

    def clone_subarray(self, project: str, variant: str, source_subarray_id: int,
                       expected_sha256: str, clone_orientation: bool = False, share_inverter: bool = False) -> dict:
        """Duplicate a grid subarray with all its exclusively owned inverter groups."""
        if not isinstance(source_subarray_id, int) or isinstance(source_subarray_id, bool):
            raise ValueError("source_subarray_id must be an integer")
        if not isinstance(clone_orientation, bool) or not isinstance(share_inverter, bool):
            raise ValueError("clone_orientation and share_inverter must be booleans")
        if share_inverter and clone_orientation:
            raise ValueError("Shared-parent cloning requires the existing common orientation")
        with self.lock:
            self._check_project(project)
            path = self._path(project, variant)
            data = self._read(path)
            self._check_hash(data, expected_sha256)
            _, lines = self._text(data)
            arrays, system = self._sections(lines)
            if source_subarray_id not in arrays or system is None:
                raise ValueError("Source subarray and grid system are required")
            system_type = self._field_indices(lines, system, "SystemType")
            if len(system_type) != 1 or FIELD.fullmatch(lines[system_type[0]].rstrip("\r\n"))[2] != "Grid":
                raise ValueError("Only a grid system is supported")
            self._require_no_geometry(lines, require_shading_section=True)
            bounds = arrays[source_subarray_id]
            orientation_fields = self._field_indices(lines, bounds, "NoOrientation")
            if len(orientation_fields) != 1:
                raise ValueError("Source subarray requires one orientation")
            orientation_id = FIELD.fullmatch(lines[orientation_fields[0]].rstrip("\r\n"))[2]
            if not orientation_id.isdigit():
                raise ValueError("Source orientation is invalid")
            orientation = self._orientation(lines, int(orientation_id))
            field_type = self._field_indices(lines, orientation, "FieldType")
            if len(field_type) != 1 or FIELD.fullmatch(lines[field_type[0]].rstrip("\r\n"))[2] != "FixedPlane":
                raise ValueError("Only a fixed-plane orientation is supported")
            matches, root = self._subarray_branches(lines, arrays, source_subarray_id)
            if share_inverter:
                from pvsyst_grid import positive_integer
                flags = root.one("Flags")[1]
                if (root.one("Version")[1] not in ("8.1.0", "8.1.6") or not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags)
                        or not int(flags[1:], 16) & 0x400 or root.one("NInverter")[1] != "1"
                        or root.one("NInvMPPT")[1] != "1" or root.one("PowerSharingInverterId")[1] != "-1"
                        or grid_inventory(lines, arrays)["counts_valid"] is not True or len(matches) != 1):
                    raise ValueError("Shared-parent cloning requires one verified independent unshared input/inverter")
                inverter = matches[0][2][0]
                if (positive_integer(inverter.one("NElements", "1"), "inverter multiplicity") != 1
                        or len(inverter.children) != 1 or inverter.children[0].kind != "MPPTNode"
                        or positive_integer(inverter.children[0].one("NElements", "1"), "MPPT multiplicity") != 1):
                    raise ValueError("Shared-parent cloning requires one direct explicit MPPT input")
                if root.one("NStrOrient1")[1] != root.one("NStringCh")[1] or any(
                        key.startswith("NStrOrient") and key != "NStrOrient1" and root.one(key)[1] != "0" for key in root.fields):
                    raise ValueError("Shared-parent cloning requires all strings in one orientation")
            current_ids = set(arrays)
            for item in self._root().iterdir():
                if (item.stem.casefold() == project[:-4].casefold()
                        and re.fullmatch(r"\.VC[A-Za-z0-9]+", item.suffix, re.I)):
                    other_path = self._path(project, item.suffix[1:])
                    _, other_lines = self._text(self._read(other_path))
                    other_arrays, _ = self._sections(other_lines)
                    current_ids.update(other_arrays)
            new_id = max(current_ids) + 1
            if new_id > 2147483647:
                raise ValueError("No available subarray ID")
            new_name = f"Sub-array #{new_id}"
            new_array = lines[bounds[0]:bounds[1] + 1].copy()
            for key, value in (("SubArrayId", str(new_id)), ("Comment", new_name)):
                index = root.one(key)[0]
                new_array[index] = replace_value(new_array[index], value)
            new_orientation_id = int(orientation_id)
            insertions = [(bounds[1] + 1, new_array)]
            if clone_orientation:
                self._require_plane_branch_mode(root, matches)
                if grid_inventory(lines, arrays)["counts_valid"] is not True:
                    raise ValueError("Independent orientation cloning requires complete consistent grid counts")
                if (root.one("NStrOrient1")[1] != root.one("NStringCh")[1] or any(
                        key.startswith("NStrOrient") and key != "NStrOrient1" and root.one(key)[1] != "0"
                        for key in root.fields)):
                    raise ValueError("Independent orientation cloning requires all strings in one orientation")
                self._ordinary_fixed_plane(lines, orientation)
                header, groups = self._orientation_list(lines)
                if [identifier for _, _, identifier in groups] != list(range(1, len(groups) + 1)):
                    raise ValueError("Independent orientation cloning requires sequential ordered IDs")
                new_orientation_id = len(groups) + 1
                if new_orientation_id > 2147483647:
                    raise ValueError("No available orientation ID")
                new_plane = lines[orientation[0]:orientation[1] + 1].copy()
                index = self._field_indices(lines, orientation, "NoOrient")[0] - orientation[0]
                new_plane[index] = replace_value(new_plane[index], str(new_orientation_id))
                index = root.one("NoOrientation")[0]
                new_array[index] = replace_value(new_array[index], str(new_orientation_id))
                lines[header] = re.sub(r"(?<==)[0-9]+", str(len(groups) + 1), lines[header], count=1)
                insertions.append((groups[-1][1] + 1, new_plane))
            for start, end, members in matches:
                if share_inverter:
                    inverter = members[0]
                    port = inverter.children[0]
                    start, end, members = port.line - 1, port.end_line - 1, members[1:]
                    lines[inverter.field_lines["SubArrayId"][0]] = replace_value(lines[inverter.field_lines["SubArrayId"][0]], "0")
                    index = inverter.field_lines["SubArrayName"][0]
                    lines[index] = replace_value(lines[index], root.one("Comment")[1] + "; " + new_name)
                branch = lines[start:end + 1].copy()
                for member in members:
                    for key, value in (("SubArrayId", str(new_id)), ("SubArrayName", new_name)):
                        index = member.field_lines[key][0] - start
                        branch[index] = replace_value(branch[index], value)
                insertions.append((end + 1, branch))
            for offset, block in sorted(insertions, key=lambda item: item[0], reverse=True):
                lines[offset:offset] = block
            candidate = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")
            _, checked = self._text(candidate)
            checked_arrays, _ = self._sections(checked)
            if new_id not in checked_arrays or len(checked_arrays) != len(arrays) + 1:
                raise ValueError("Subarray structure validation failed")
            if share_inverter:
                inventory = grid_inventory(checked, checked_arrays)
                groups = [group for group in inventory["inverter_groups"] if group.get("subarray_ids") == [source_subarray_id, new_id]]
                if inventory["counts_valid"] is not True or len(groups) != 1:
                    raise ValueError("Shared-parent clone failed complete allocation validation")
            else:
                new_matches, _ = self._subarray_branches(checked, checked_arrays, new_id)
                if len(new_matches) != len(matches):
                    raise ValueError("Subarray inverter groups were not completely copied")
            if clone_orientation:
                self._orientation_list(checked)
                self._orientation(checked, new_orientation_id)
            if self._read(path) != data:
                raise ValueError("Variant changed while preparing the edit")
            backup = self._backup(path, data)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(candidate)
                if self._read(path) != data:
                    raise ValueError("Variant changed while preparing the edit")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {"project": project, "variant": variant.upper(), "subarray_id": new_id,
                    "orientation_id": new_orientation_id, "orientation_cloned": clone_orientation,
                    "inverter_group_count": len(matches), "shared_parent_created": share_inverter,
                    "backup_name": backup, "sha256": hashlib.sha256(candidate).hexdigest()}

    def remove_subarray(self, project: str, variant: str, subarray_id: int,
                        expected_sha256: str, remove_orientation: bool = False, shared_member: bool = False) -> dict:
        """Remove a subarray and its owned groups, retaining an orientation still in use."""
        if not isinstance(subarray_id, int) or isinstance(subarray_id, bool):
            raise ValueError("subarray_id must be an integer")
        if not isinstance(remove_orientation, bool) or not isinstance(shared_member, bool):
            raise ValueError("remove_orientation and shared_member must be booleans")
        if shared_member and remove_orientation:
            raise ValueError("Shared-member deletion preserves the common orientation")
        with self.lock:
            self._check_project(project)
            path = self._path(project, variant)
            data = self._read(path)
            self._check_hash(data, expected_sha256)
            _, lines = self._text(data)
            arrays, system = self._sections(lines)
            if subarray_id not in arrays or len(arrays) < 2 or system is None:
                raise ValueError("A target and another remaining subarray are required")
            kinds = self._field_indices(lines, system, "SystemType")
            if len(kinds) != 1 or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid":
                raise ValueError("Only grid subarrays are supported")
            self._require_no_geometry(lines, require_shading_section=True)
            bounds = arrays[subarray_id]
            no_orient = self._field_indices(lines, bounds, "NoOrientation")
            if len(no_orient) != 1:
                raise ValueError("Subarray needs one orientation")
            orientation = FIELD.fullmatch(lines[no_orient[0]].rstrip("\r\n"))[2]
            if not orientation.isdigit() or not 1 <= int(orientation) <= 2147483647:
                raise ValueError("Orientation ID must be a positive 32-bit integer")
            shared = any(other != subarray_id and any(
                    (raw := FIELD.fullmatch(lines[i].rstrip("\r\n"))[2]).isdigit() and int(raw) == int(orientation)
                    for i in self._field_indices(lines, other_bounds, "NoOrientation"))
                    for other, other_bounds in arrays.items())
            if shared and remove_orientation:
                raise ValueError("Orientation is still referenced by another subarray")
            if not shared and not remove_orientation:
                raise ValueError("Orientation must remain in use by another subarray")
            remaining_member = None
            if shared_member:
                checked = grid_inventory(lines, arrays)
                groups = [group for group in checked["inverter_groups"] if subarray_id in group.get("subarray_ids", [])]
                if (checked["counts_valid"] is not True or len(groups) != 1
                        or len(groups[0]["subarray_ids"]) != 2 or groups[0]["power_sharing_id"] != -1
                        or groups[0]["inverters"] != 1):
                    raise ValueError("Shared-member deletion requires one complete disabled two-member parent")
                member_ids = groups[0]["subarray_ids"]
                roots = {}
                for identifier, (first, last) in arrays.items():
                    peer = next(block for block in objects("".join(lines[first:last + 1])) if not block.path)
                    if "PowerSharingInverterId" in peer.fields and peer.one("PowerSharingInverterId")[1] != "-1":
                        raise ValueError("Disable all power sharing before shared-member deletion")
                    if identifier in member_ids:
                        row = next(row for row in checked["subarrays"] if row["id"] == identifier)
                        if (peer.one("NInverter")[1] != "1" or peer.one("NInvMPPT")[1] != "1"
                                or peer.one("NoOrientation")[1] != orientation or row["circuit"]["inverters"] != 1
                                or len(row["shared_inverter_lines"]) != 1
                                or peer.one("NStrOrient1")[1] != peer.one("NStringCh")[1]
                                or any(key.startswith("NStrOrient") and key != "NStrOrient1" and peer.one(key)[1] != "0" for key in peer.fields)):
                            raise ValueError("Shared-member deletion requires single-input members in a common plane")
                        roots[identifier] = peer
                plane = self._orientation(lines, int(orientation))
                types = self._field_indices(lines, plane, "FieldType")
                if len(types) != 1 or FIELD.fullmatch(lines[types[0]].rstrip("\r\n"))[2] != "FixedPlane":
                    raise ValueError("Shared-member deletion requires a fixed plane")
                def walk(node):
                    yield node
                    for child in node.children:
                        yield from walk(child)
                parent = next(node for tree in circuit_nodes(lines) for node in walk(tree) if node.line == groups[0]["line"])
                if parent.one("SubArrayId") != "0":
                    raise ValueError("Shared-member deletion requires explicit parent owner 0")
                port = next(node for node in parent.children if node.one("SubArrayId") == str(subarray_id))
                owned = {bounds[0] + roots[subarray_id].one("SubArrayId")[0]}
                owned.update(index for node in walk(port) for index in node.field_lines["SubArrayId"])
                references = {i for i, line in enumerate(lines) if (field := FIELD.fullmatch(line.rstrip("\r\n")))
                              and field[1] == "SubArrayId" and field[2] == str(subarray_id)}
                if not references.issubset(owned):
                    raise ValueError("Shared member has references outside its owned subtree")
                remaining_member = next(identifier for identifier in member_ids if identifier != subarray_id)
                for key, value in (("SubArrayId", str(remaining_member)), ("SubArrayName", roots[remaining_member].one("Comment")[1])):
                    index = parent.field_lines[key][0]
                    lines[index] = replace_value(lines[index], value)
                root = roots[subarray_id]
                matches = [(port.line - 1, port.end_line - 1, list(walk(port)))]
            else:
                matches, root = self._subarray_branches(lines, arrays, subarray_id)
            deletions = [bounds] + [(start, end) for start, end, _ in matches]
            orientation_id_map = {}
            if remove_orientation:
                counts = grid_inventory(lines, arrays)
                if counts["counts_valid"] is not True:
                    raise ValueError("Orientation removal requires complete consistent grid counts")
                header, groups = self._orientation_list(lines)
                if len(groups) < 2:
                    raise ValueError("Another orientation must remain")
                if [identifier for _, _, identifier in groups] != list(range(1, len(groups) + 1)):
                    raise ValueError("Orientation removal requires sequential ordered IDs")
                plane = self._orientation(lines, int(orientation))
                fields = self._field_indices(lines, plane, "FieldType")
                if len(fields) != 1 or FIELD.fullmatch(lines[fields[0]].rstrip("\r\n"))[2] != "FixedPlane":
                    raise ValueError("Only a fixed-plane orientation can be removed")
                references = [i for i, line in enumerate(lines) if (field := FIELD.fullmatch(line.rstrip("\r\n")))
                              and field[1] == "NoOrientation" and field[2].isdigit() and int(field[2]) == int(orientation)]
                if references != [bounds[0] + root.one("NoOrientation")[0]]:
                    raise ValueError("Orientation has references outside its owning subarray")
                owned_references = set()
                for first, last, identifier in groups:
                    self._ordinary_fixed_plane(lines, (first, last))
                    index = self._field_indices(lines, (first, last), "NoOrient")[0]
                    owned_references.add(index)
                    if identifier > int(orientation):
                        orientation_id_map[str(identifier)] = identifier - 1
                for identifier, (first, last) in arrays.items():
                    array_root = root if identifier == subarray_id else next(
                        block for block in objects("".join(lines[first:last + 1])) if not block.path)
                    owned_branches, _ = self._subarray_branches(lines, arrays, identifier)
                    self._require_plane_branch_mode(array_root, owned_branches)
                    if (array_root.one("NStrOrient1")[1] != array_root.one("NStringCh")[1] or any(
                            key.startswith("NStrOrient") and key != "NStrOrient1" and array_root.one(key)[1] != "0"
                            for key in array_root.fields)):
                        raise ValueError("Orientation removal requires all strings in one orientation")
                    owned_references.add(first + array_root.one("NoOrientation")[0])
                for index, line in enumerate(lines):
                    field = FIELD.fullmatch(line.rstrip("\r\n"))
                    if field and (field[1].startswith("NoOrient") or field[1] == "OrientationId"):
                        if index not in owned_references:
                            raise ValueError("Orientation removal has references outside owned records")
                if orientation_id_map:
                    for index in owned_references:
                        raw = FIELD.fullmatch(lines[index].rstrip("\r\n"))[2]
                        if str(int(raw)) in orientation_id_map:
                            lines[index] = replace_value(lines[index], str(orientation_id_map[str(int(raw))]))
                lines[header] = re.sub(r"(?<==)[0-9]+", str(len(groups) - 1), lines[header], count=1)
                deletions.append(plane)
            for first, last in sorted(deletions, reverse=True):
                del lines[first:last + 1]
            candidate = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")
            _, checked = self._text(candidate)
            checked_arrays, _ = self._sections(checked)
            if subarray_id in checked_arrays or len(checked_arrays) != len(arrays) - 1:
                raise ValueError("Subarray removal failed validation")
            if shared_member:
                self._subarray_branches(checked, checked_arrays, remaining_member)
            if (len(matches) > 1 or remove_orientation or shared_member) and grid_inventory(checked, checked_arrays)["counts_valid"] is not True:
                raise ValueError("Remaining grouped grid counts failed validation")
            if remove_orientation:
                self._orientation_list(checked)
            if self._read(path) != data:
                raise ValueError("Variant changed while preparing the edit")
            backup = self._backup(path, data)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(candidate)
                if self._read(path) != data:
                    raise ValueError("Variant changed while preparing the edit")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {"project": project, "variant": variant.upper(), "removed_subarray_id": subarray_id,
                    "removed_orientation_id": int(orientation) if remove_orientation else None,
                    "orientation_id_map": orientation_id_map, "remaining_member_id": remaining_member,
                    "inverter_group_count": len(matches),
                    "backup_name": backup, "sha256": hashlib.sha256(candidate).hexdigest()}

    def _archive_folder(self) -> Path:
        root = self._root()
        folder = root / ".mcp-project-archive"
        if folder.is_symlink() or folder.resolve().parent != root:
            raise ValueError("Project archive resolves outside workspace")
        folder.mkdir(exist_ok=True)
        return folder

    @staticmethod
    def _embedded_reference(lines: list[str], header: str, closing: str,
                            field_name: str = "NomF") -> str | None:
        starts = [i for i, line in enumerate(lines) if line.strip() == header]
        if not starts:
            return None
        if len(starts) != 1:
            raise ValueError(f"Multiple {header} sections")
        end = next((i for i in range(starts[0] + 1, len(lines))
                    if lines[i].strip() == closing), None)
        if end is None:
            raise ValueError(f"Unclosed {header} section")
        child = next((i for i in range(starts[0] + 1, end)
                      if lines[i].strip().startswith("PVObject_")), end)
        indices = VariantStore._field_indices(lines, (starts[0], child), field_name)
        if len(indices) != 1:
            raise ValueError(f"{header} requires one {field_name}")
        return FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]

    def _workspace_asset(self, directory: str, name: str | None) -> dict:
        if name is None:
            return {"name": None, "workspace_file": False}
        if (not name or Path(name).name != name or "\\" in name or "/" in name
                or not name.lower().endswith({"Sites": ".sit", "Meteo": ".met"}[directory])):
            return {"name": name, "workspace_file": False}
        base = self.workspace / directory
        path = base / name
        found = (base.is_dir() and not base.is_symlink() and base.resolve().parent == self.workspace
                 and not path.is_symlink()
                 and path.is_file() and path.resolve().parent == base.resolve())
        return {"name": name, "workspace_file": found}

    def inspect_project(self, project: str) -> dict:
        with self.lock:
            self._check_project(project)
            members = [self._path(project)]
            for path in self._root().iterdir():
                if path.name.casefold() == project.casefold():
                    continue
                if self._belongs_to_project(path, project):
                    if not re.fullmatch(r"VC[A-Za-z0-9]+", path.suffix[1:], re.I):
                        raise ValueError(f"Unrecognized project sidecar: {path.name}")
                    members.append(self._path(project, path.suffix[1:]))
            hashes = {}
            sources = {}
            warnings = []
            for path in members:
                data = self._read(path)
                if path.suffix.lower() == ".prj":
                    lines = data.decode("utf-8-sig").splitlines()
                    names = self._display_name_fields(lines)
                    project_name = FIELD.fullmatch(lines[names[0]])[2] if len(names) == 1 else None
                    project_bom = data.startswith(b"\xef\xbb\xbf")
                    meteo_fields = [field[2] for line in lines
                                    if (field := FIELD.fullmatch(line)) and field[1] == "MeteoFileName"]
                    if len(meteo_fields) > 1:
                        raise ValueError("Project has multiple MeteoFileName fields")
                    site = self._embedded_reference(lines, "PVObject_SitePrj=pvSite",
                                                    "End of PVObject pvSite")
                    meteo = meteo_fields[0] if meteo_fields else None
                else:
                    _, lines = self._text(data)
                    self._sections(lines)
                    site = self._embedded_reference(lines, "PVObject_SiteSimul=pvSite",
                                                    "End of PVObject pvSite")
                    meteo = self._embedded_reference(lines, "PVObject_MeteoSimul=pvMeteo",
                                                     "End of PVObject pvMeteo")
                hashes[path.name] = hashlib.sha256(data).hexdigest()
                sources[path.name] = {"site": self._workspace_asset("Sites", site),
                                      "meteo": self._workspace_asset("Meteo", meteo)}
                for kind, asset in sources[path.name].items():
                    if asset["name"] and not asset["workspace_file"]:
                        warnings.append(f"{path.name}: {kind} {asset['name']} is not a workspace file")
            project_refs = sources[members[0].name]
            for path in members[1:]:
                refs = sources[path.name]
                for kind in ("site", "meteo"):
                    expected, actual = project_refs[kind]["name"], refs[kind]["name"]
                    if expected and actual and expected.casefold() != actual.casefold():
                        warnings.append(f"{path.name}: {kind} differs from {members[0].name}")
            return {"project": project, "project_name": project_name, "utf8_bom": project_bom, "files": hashes,
                    "variants": [path.suffix[1:] for path in members[1:]],
                    "sources": sources, "warnings": warnings}

    @staticmethod
    def _replace_site_block(lines: list[str], role: str, site_lines: list[str]) -> None:
        header = f"PVObject_{role}=pvSite"
        starts = [i for i, line in enumerate(lines) if line.strip() == header]
        if len(starts) != 1:
            raise ValueError(f"Expected one {header} section")
        start = starts[0]
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].strip() == "End of PVObject pvSite"), None)
        if end is None or any(line.strip().startswith("PVObject_")
                              for line in lines[start + 1:end]):
            raise ValueError(f"Unsupported nested {header} section")
        prefix = lines[start][:len(lines[start]) - len(lines[start].lstrip())]
        ending = "\r\n" if lines[start].endswith("\r\n") else "\n"
        lines[start:end + 1] = [prefix + header + ending] + [
            prefix + line.rstrip("\r\n") + ending for line in site_lines[1:-1]
        ] + [prefix + "End of PVObject pvSite" + ending]

    @staticmethod
    def _replace_unique_field(lines: list[str], key: str, value: str,
                              bounds: tuple[int, int]) -> None:
        indices = [i for i in range(bounds[0], bounds[1])
                   if (field := FIELD.fullmatch(lines[i].rstrip("\r\n"))) and field[1] == key]
        if len(indices) != 1:
            raise ValueError(f"Expected one {key} in target section")
        index = indices[0]
        original = lines[index]
        ending = "\r\n" if original.endswith("\r\n") else "\n" if original.endswith("\n") else ""
        lines[index] = original[:original.index("=") + 1] + value + ending

    @staticmethod
    def _section_bounds(lines: list[str], header: str, closing: str) -> tuple[int, int]:
        starts = [i for i, line in enumerate(lines) if line.strip() == header]
        if len(starts) != 1:
            raise ValueError(f"Expected one {header} section")
        end = next((i for i in range(starts[0] + 1, len(lines))
                    if lines[i].strip() == closing), None)
        if end is None:
            raise ValueError(f"Unclosed {header} section")
        return starts[0], end

    def _commit_project_files(self, project: str, originals: dict[Path, bytes],
                              candidates: dict[Path, bytes]) -> dict:
        if not originals or originals.keys() != candidates.keys():
            raise ValueError("Project transaction must contain the same complete file set")
        for data in (*originals.values(), *candidates.values()):
            self._check_size(data)
        transaction_id = uuid.uuid4().hex
        manifest_name = f"transaction.{transaction_id}.json"
        backups = {}
        staged = {}
        written = []
        try:
            for path, data in originals.items():
                backups[path.name] = self._backup(path, data, transaction_id)
            with (self._backup_folder() / manifest_name).open("x", encoding="utf-8") as stream:
                json.dump({"version": 1, "transaction_id": transaction_id, "project": project,
                           "backups": backups, "original_files": {
                               path.name: hashlib.sha256(data).hexdigest()
                               for path, data in originals.items()}}, stream)
            for path, data in candidates.items():
                if data == originals[path]:
                    continue
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                staged[path] = temporary
                with temporary.open("xb") as stream:
                    stream.write(data)
            expected = {path.name: hashlib.sha256(data).hexdigest()
                        for path, data in originals.items()}
            if self.inspect_project(project)["files"] != expected:
                raise ValueError("Project files changed while preparing the transaction")
            for path, temporary in staged.items():
                os.replace(temporary, path)
                written.append(path)
        except BaseException:
            for path in reversed(written):
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                with temporary.open("xb") as stream:
                    stream.write(originals[path])
                os.replace(temporary, path)
            raise
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)
        return {"backups": backups, "transaction_id": transaction_id,
                "transaction_manifest": manifest_name,
                "files": {path.name: hashlib.sha256(data).hexdigest()
                          for path, data in candidates.items()}}

    def update_project_parameters(self, project: str, expected_files: dict[str, str],
                                  project_name: str, dry_run: bool = False,
                                  add_utf8_bom: bool = False) -> dict:
        self._validate_display_name(project_name, "project_name")
        if not isinstance(dry_run, bool) or not isinstance(add_utf8_bom, bool):
            raise ValueError("dry_run and add_utf8_bom must be booleans")
        with self.lock:
            inventory = self.inspect_project(project)
            if inventory["files"] != expected_files:
                raise ValueError("Project files changed since inspection")
            originals = {}
            for name, digest in expected_files.items():
                path = self._path(project) if name == project else self._path(project, name[len(project[:-4]) + 1:])
                data = self._read(path)
                self._check_hash(data, digest)
                originals[path] = data
            path = self._path(project)
            data = originals[path]
            bom = data.startswith(b"\xef\xbb\xbf")
            prefix = self._display_name_prefix(data, project_name, add_utf8_bom, "project_name")
            text = data.decode("utf-8-sig")
            lines = text.splitlines(keepends=True)
            index, = self._display_name_fields(lines, complete=True)
            name_before = FIELD.fullmatch(lines[index].rstrip("\r\n"))[2]
            lines[index] = replace_value(lines[index], project_name)
            candidate = prefix + "".join(lines).encode("utf-8")
            self._project_text(candidate)
            candidates = {**originals, path: candidate}
            diff = "".join(itertools.islice(difflib.unified_diff(text.splitlines(keepends=True), lines,
                fromfile=path.name, tofile=path.name + " (candidate)"), 501))
            result = {"project": project, "before_files": inventory["files"],
                "files": {p.name: hashlib.sha256(value).hexdigest() for p, value in candidates.items()},
                "changed": candidate != data, "dry_run": dry_run,
                "diff": diff[:32000], "diff_truncated": len(diff) > 32000 or len(diff.splitlines()) > 500,
                "name_change": {"before": name_before, "after": project_name},
                "bom_added": add_utf8_bom and not bom}
            if dry_run or candidate == data:
                return result
            return {**result, **self._commit_project_files(project, originals, candidates)}

    def update_project_sources(self, project: str, site_name: str, met_name: str,
                               expected_files: dict[str, str]) -> dict:
        """Rebind PRJ and every VC to one workspace SIT/MET pair."""
        with self.lock:
            inventory = self.inspect_project(project)
            if inventory["files"] != expected_files or len(expected_files) < 2:
                raise ValueError("Project files changed since inspection or has no variants")
            site_ref = self._workspace_asset("Sites", site_name)
            met_ref = self._workspace_asset("Meteo", met_name)
            if not site_ref["workspace_file"] or not met_ref["workspace_file"]:
                raise ValueError("SIT and MET must be existing workspace files")
            site_path = self.workspace / "Sites" / site_name
            site_bytes = self._read(site_path)
            try:
                site_text = site_bytes.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError("SIT must be UTF-8 text") from exc
            if "\x00" in site_text:
                raise ValueError("SIT contains NUL bytes")
            site_lines = site_text.splitlines(keepends=True)
            if (len(site_lines) < 3 or site_lines[0].strip() != "PVObject_=pvSite"
                    or site_lines[-1].strip() != "End of PVObject pvSite"):
                raise ValueError("SIT is not a supported pvSite object")
            if any(line.strip().startswith(("PVObject_", "End of PVObject"))
                   for line in site_lines[1:-1]):
                raise ValueError("SIT must contain one non-nested pvSite object")
            identifiers = [field[2] for line in site_lines
                           if (field := FIELD.fullmatch(line.rstrip("\r\n"))) and field[1] == "Site"]
            filenames = [field[2] for line in site_lines
                         if (field := FIELD.fullmatch(line.rstrip("\r\n"))) and field[1] == "NomF"]
            if len(identifiers) != 1 or not identifiers[0] or filenames != [site_name]:
                raise ValueError("SIT site identity or filename does not match")
            site_identity = identifiers[0]
            originals = {}
            candidates = {}
            for name, digest in expected_files.items():
                path = self._path(project) if name == project else self._path(project, name[len(project[:-4]) + 1:])
                data = self._read(path)
                self._check_hash(data, digest)
                text = data.decode("utf-8-sig")
                lines = text.splitlines(keepends=True)
                if name == project:
                    self._replace_site_block(lines, "SitePrj", site_lines)
                    self._replace_unique_field(lines, "MeteoFileName", met_name, (0, len(lines)))
                else:
                    self._text(data)
                    self._replace_site_block(lines, "SiteSimul", site_lines)
                    self._replace_site_block(lines, "SiteMet", site_lines)
                    start, end = self._section_bounds(lines, "PVObject_MeteoSimul=pvMeteo",
                                                      "End of PVObject pvMeteo")
                    child = next((i for i in range(start + 1, end)
                                  if lines[i].strip().startswith("PVObject_")), end)
                    self._replace_unique_field(lines, "NomF", met_name, (start + 1, child))
                    self._replace_unique_field(lines, "SiteM", site_identity, (start + 1, child))
                candidate = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")
                self._check_size(candidate)
                if name != project:
                    self._text(candidate)
                    self._sections(self._text(candidate)[1])
                originals[path] = data
                candidates[path] = candidate
            transaction = self._commit_project_files(project, originals, candidates)
            return {"project": project, "site": site_name, "meteo": met_name,
                    "note": "Historical weather origin fields remain from the source variant",
                    **transaction}

    def restore_project_sources(self, project: str, backups: dict[str, str],
                                expected_files: dict[str, str], confirm: bool = False) -> dict:
        """Restore every PRJ/VC snapshot from a source/name project transaction."""
        if not confirm:
            raise ValueError("Restore requires confirm=true")
        with self.lock:
            inventory = self.inspect_project(project)["files"]
            if inventory != expected_files or set(backups) != set(expected_files):
                raise ValueError("Project files changed since inspection or backup set is incomplete")
            transaction_ids = set()
            for name, backup_name in backups.items():
                match = (re.fullmatch(re.escape(name) + r"\.([0-9a-f]{32})\.bak", backup_name)
                         if isinstance(backup_name, str) else None)
                if match is None:
                    raise ValueError("Backup name must match each project file")
                transaction_ids.add(match[1])
            if len(transaction_ids) != 1:
                raise ValueError("Backups must belong to one complete source transaction")
            transaction_id = transaction_ids.pop()
            folder = self._backup_folder()
            manifest_path = folder / f"transaction.{transaction_id}.json"
            if (manifest_path.is_symlink() or manifest_path.resolve().parent != folder
                    or not manifest_path.is_file()):
                raise ValueError("Missing or unsafe source transaction manifest; legacy sets require manual recovery")
            manifest = json.loads(self._read(manifest_path).decode("utf-8"))
            if (not isinstance(manifest, dict) or manifest.get("version") != 1
                    or manifest.get("project") != project
                    or manifest.get("transaction_id") != transaction_id
                    or manifest.get("backups") != backups
                    or not isinstance(manifest.get("original_files"), dict)
                    or set(manifest["original_files"]) != set(backups)
                    or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                           for value in manifest["original_files"].values())):
                raise ValueError("Backups must match one complete source transaction manifest")
            originals = {}
            candidates = {}
            for name, digest in expected_files.items():
                path = self._path(project) if name == project else self._path(project, name[len(project[:-4]) + 1:])
                data = self._read(path)
                self._check_hash(data, digest)
                backup_name = backups[name]
                if (not isinstance(backup_name, str) or not re.fullmatch(
                        re.escape(name) + r"\.[0-9a-f]{32}\.bak", backup_name)):
                    raise ValueError("Backup name must match each project file")
                snapshot = self._backup_folder() / backup_name
                if snapshot.is_symlink() or snapshot.resolve().parent != self._backup_folder():
                    raise ValueError("Linked project backup is not supported")
                previous = self._read(snapshot)
                if hashlib.sha256(previous).hexdigest() != manifest["original_files"][name]:
                    raise ValueError(f"Project backup integrity check failed: {name}")
                if name == project:
                    lines = previous.decode("utf-8-sig").splitlines()
                    if (not lines or lines[0].strip() != "PVObject_=pvProject"
                            or lines[-1].strip() != "End of PVObject pvProject"):
                        raise ValueError("Archived project snapshot is malformed")
                else:
                    _, lines = self._text(previous)
                    self._sections(lines)
                originals[path] = data
                candidates[path] = previous
            return {"project": project, **self._commit_project_files(project, originals, candidates)}

    def archive_project(self, project: str, expected_files: dict[str, str],
                        confirm: bool = False) -> dict:
        if not confirm:
            raise ValueError("Archive requires confirm=true")
        with self.lock:
            actual = self.inspect_project(project)["files"]
            if actual != expected_files or len(actual) < 2:
                raise ValueError("Project files changed since inspection or has no variants")
            if len(actual) > MAX_ARCHIVE_MEMBERS:
                raise ValueError(f"Project archive exceeds {MAX_ARCHIVE_MEMBERS} members")
            folder = self._archive_folder() / uuid.uuid4().hex
            folder.mkdir()
            moved = []
            try:
                with (folder / "manifest.json").open("x", encoding="utf-8") as stream:
                    json.dump({"project": project, "files": actual}, stream)
                for name in actual:
                    path = self._root() / name
                    os.replace(path, folder / name)
                    moved.append(name)
            except BaseException:
                for name in reversed(moved):
                    os.replace(folder / name, self._root() / name)
                (folder / "manifest.json").unlink(missing_ok=True)
                folder.rmdir()
                raise
            return {"project": project, "archive_name": folder.name, "files": list(actual)}

    def archive_variant(self, project: str, variant: str, expected_sha256: str,
                        confirm: bool = False) -> dict:
        if not confirm:
            raise ValueError("Archive requires confirm=true")
        with self.lock:
            self._check_project(project)
            path = self._path(project, variant)
            data = self._read(path)
            self._check_hash(data, expected_sha256)
            _, lines = self._text(data)
            self._sections(lines)
            members = self.inspect_project(project)["files"]
            others = [name for name in members
                      if name.casefold() not in (project.casefold(), path.name.casefold())]
            if not others:
                raise ValueError("Archive the project instead of its last variant")
            archive_name = uuid.uuid4().hex
            target = self._archive_folder() / archive_name
            target.mkdir()
            try:
                with (target / "manifest.json").open("x", encoding="utf-8") as stream:
                    json.dump({"project": project, "files": {path.name: expected_sha256.lower()}}, stream)
                os.replace(path, target / path.name)
            except BaseException:
                if (target / path.name).exists():
                    os.replace(target / path.name, path)
                (target / "manifest.json").unlink(missing_ok=True)
                target.rmdir()
                raise
            return {"project": project, "variant": variant.upper(), "archive_name": archive_name}

    def restore_archive(self, project: str, archive_name: str, confirm: bool = False) -> dict:
        if not confirm:
            raise ValueError("Restore requires confirm=true")
        if not isinstance(archive_name, str) or not re.fullmatch(r"[0-9a-f]{32}", archive_name):
            raise ValueError("Invalid archive name")
        with self.lock:
            folder = self._archive_folder() / archive_name
            if folder.is_symlink() or folder.resolve().parent != self._archive_folder():
                raise ValueError("Archive resolves outside workspace")
            with (folder / "manifest.json").open("r", encoding="utf-8") as stream:
                manifest = json.load(stream)
            if manifest.get("project") != project or not isinstance(manifest.get("files"), dict):
                raise ValueError("Archive belongs to another project")
            files = manifest["files"]
            if not files or len(files) > MAX_ARCHIVE_MEMBERS or any(not isinstance(name, str) or not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest) for name, digest in files.items()):
                raise ValueError("Invalid archive manifest")
            sources = []
            for name, digest in files.items():
                if name == project:
                    path = self._path(project)
                elif name.startswith(project[:-4] + "."):
                    path = self._path(project, name[len(project[:-4]) + 1:])
                else:
                    raise ValueError("Archive filename does not match project")
                source = folder / name
                if source.is_symlink() or source.resolve().parent != folder or path.exists():
                    raise ValueError("Archive has linked data or destination already exists")
                data = self._read(source)
                self._check_hash(data, digest)
                if name == project:
                    if data.decode("utf-8-sig").splitlines()[0].strip() != "PVObject_=pvProject":
                        raise ValueError("Archived project is malformed")
                else:
                    _, lines = self._text(data)
                    self._sections(lines)
                sources.append((source, path))
            if {p.name for p in folder.iterdir()} != {"manifest.json", *files}:
                raise ValueError("Archive contains unexpected files")
            moved = []
            try:
                for source, target in sources:
                    os.replace(source, target)
                    moved.append((source, target))
            except BaseException:
                for source, target in reversed(moved):
                    os.replace(target, source)
                raise
            (folder / "manifest.json").unlink()
            folder.rmdir()
            return {"project": project, "restored": list(files)}

    def restore(self, project: str, variant: str, backup_name: str,
                expected_sha256: str, confirm: bool = False) -> dict:
        if not confirm:
            raise ValueError("Restore requires confirm=true")
        with self.lock:
            self._check_project(project)
            path = self._path(project, variant)
            data = self._read(path)
            self._check_hash(data, expected_sha256)
            if not isinstance(backup_name, str) or not re.fullmatch(
                    re.escape(path.name) + r"\.[0-9a-f]{32}\.bak", backup_name):
                raise ValueError("Backup name must match the variant")
            backup = self._backup_folder() / backup_name
            if backup.is_symlink() or backup.resolve().parent != self._backup_folder():
                raise ValueError("Symlink backups are not supported")
            previous_data = self._read(backup)
            _, lines = self._text(previous_data)
            self._sections(lines)
            previous_backup = self._backup(path, data)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(previous_data)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {"project": project, "variant": variant.upper(), "backup_name": previous_backup,
                    "restored_from": backup_name, "sha256": hashlib.sha256(previous_data).hexdigest()}
