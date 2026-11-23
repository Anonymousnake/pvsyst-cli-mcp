"""Guarded project copies, lifecycle operations, and PVsyst variant storage."""
from __future__ import annotations

import difflib
import hashlib
import itertools
import json
import os
import re
import threading
import uuid
from pathlib import Path

from pvsyst_auto.components.editor import objects, replace_value, root_block
from pvsyst_auto.components.store import ComponentStore, FIELD, MAX_PROJECT_BYTES
from pvsyst_auto.project import fileio, variant_readback, variant_topology, variant_updates
from pvsyst_auto.project.paths import battery_reference_name, project_path, project_root
from pvsyst_auto.versions import NATIVE_TEXT_OR_SEED, SEED_LABEL

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
                    indices = VariantStore._field_indices(lines, bounds, "SubArrayId")
                    if len(indices) != 1:
                        raise ValueError("Subarray requires one SubArrayId")
                    identifier = FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]
                    if not identifier.isdigit() or int(identifier) < 1 or int(identifier) in arrays:
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
        first = lines[bounds[0]].strip()
        if first in (*STARTS.values(), "PVObject_PVMainArray=pvMainArray"):
            root = next(block for block in objects("".join(lines[bounds[0]:bounds[1] + 1])) if not block.path)
            return [bounds[0] + index for index, _ in root.fields.get(key, [])]
        if first == "PVObject_=pvOrient":
            depth = 0
            found: list[int] = []
            for index in range(bounds[0] + 1, bounds[1]):
                stripped = lines[index].strip()
                if stripped.startswith("PVObject_"):
                    depth += 1
                    continue
                if stripped.startswith("End of PVObject "):
                    depth = max(0, depth - 1)
                    continue
                if depth == 0 and (match := FIELD.fullmatch(lines[index].rstrip("\r\n"))) and match[1] == key:
                    found.append(index)
            return found
        return [index for index in range(bounds[0] + 1, bounds[1])
                if (match := FIELD.fullmatch(lines[index].rstrip("\r\n"))) and match[1] == key]

    @staticmethod
    def _display_name_fields(lines: list[str], complete: bool = False) -> list[int]:
        child = next((i for i in range(1, len(lines)) if lines[i].strip().startswith("PVObject_")
                      or re.match(r"\s*PrintSettings\s*=", lines[i])), len(lines) - 1)
        indices = VariantStore._field_indices(lines, (0, child), "Comment")
        if complete:
            root = root_block(lines)
            if len(indices) != 1 or indices != [i for i, _ in root.fields.get("Comment", [])]:
                raise ValueError("Display name requires one owning header Comment")
        return indices

    @staticmethod
    def _validate_display_name(value: str, field: str) -> None:
        if (not isinstance(value, str) or not value or value != value.strip()
                or not value.isprintable() or len(value.encode("utf-8")) > 80):
            raise ValueError(f"Invalid {field}: require 1..80 UTF-8 bytes of printable text without surrounding whitespace")

    @staticmethod
    def _display_name_prefix(data: bytes, name: str, add_utf8_bom: bool, field: str,
                             content: str | None = None) -> bytes:
        bom = data.startswith(b"\xef\xbb\xbf")
        if not name.isascii() and not bom and not add_utf8_bom:
            raise ValueError(f"Non-ASCII {field} values require UTF-8 BOM; use add_utf8_bom=true explicitly")
        if content is not None and not content.isascii():
            return b"\xef\xbb\xbf"
        return b"\xef\xbb\xbf" if bom or add_utf8_bom else b""

    @staticmethod
    def _check_hash(data: bytes, expected: str | None) -> None:
        if expected is None:
            return
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
        text, lines = self._text(data)
        arrays, system = self._sections(lines)
        for kind, filename in updates.items():
            if kind == "BTR":
                battery_reference_name(filename)
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
            if kind == "BTR":
                key, equals, old = line.partition("=")
                line = key + equals + old.lstrip(" \t")
            lines[index] = replace_value(line, filename)
        if "".join(lines) == text:
            return data

        from pvsyst_auto.native.headless import strip_result_cache

        lines, _ = strip_result_cache(lines)
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

    # Documented pvMainArray.Flags bits toggled by MV/HV transformers, AC cabling, and default-loss overrides
    _VERIFIED_MAIN_FLAG_MASK = 0x21EC0000

    @classmethod
    def _is_verified_main_flags(cls, raw: str) -> bool:
        if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", raw):
            return False
        return (int(raw[1:], 16) & ~cls._VERIFIED_MAIN_FLAG_MASK) in (0x00100400, 0x00000400)

    @staticmethod
    def _ordinary_fixed_plane(lines, bounds, allow_missing_flags=False, allow_sheds=False):
        _allowed_nested = ("PVObject_Bifacial=", "PVObject_BiFacialShedModel=", "PVObject_BiFacialTrackerModel=")
        if any((line.strip().startswith("PVObject_") and not line.strip().startswith(_allowed_nested))
               or line.strip().endswith("=Start")
               for line in lines[bounds[0] + 1:bounds[1]]):
            raise ValueError("Operation requires a flat fixed-plane record")
        allowed_types = ("FixedPlane", "Sheds") if allow_sheds else ("FixedPlane",)
        ft_indices = VariantStore._field_indices(lines, bounds, "FieldType")
        if len(ft_indices) != 1 or FIELD.fullmatch(lines[ft_indices[0]].rstrip("\r\n"))[2] not in allowed_types:
            raise ValueError("Operation requires ordinary FieldType=FixedPlane" + (" or Sheds" if allow_sheds else ""))
        fl_indices = VariantStore._field_indices(lines, bounds, "Flags")
        if fl_indices or not allow_missing_flags:
            if len(fl_indices) != 1 or FIELD.fullmatch(lines[fl_indices[0]].rstrip("\r\n"))[2] != "$00":
                raise ValueError("Operation requires ordinary Flags=$00")
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

    @staticmethod
    def _uv_edit_gate(lines, system, arrays, main_array) -> None:
        kinds = VariantStore._field_indices(lines, system, "SystemType") if system is not None else []
        versions = [VariantStore._field_indices(lines, bounds, "Version") for bounds in arrays.values()]
        if (main_array is None or len(kinds) != 1
                or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid"
                or not versions or any(len(indices) != 1
                                       or FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2] not in NATIVE_TEXT_OR_SEED
                                       for indices in versions)):
            raise ValueError(f"UVFactor editing requires a verified {SEED_LABEL} grid main array")
        flags = VariantStore._field_indices(lines, main_array, "Flags")
        flags2 = VariantStore._field_indices(lines, main_array, "Flags2")
        if (len(flags) != 1 or not VariantStore._is_verified_main_flags(FIELD.fullmatch(lines[flags[0]].rstrip("\r\n"))[2])
                or len(flags2) != 1 or FIELD.fullmatch(lines[flags2[0]].rstrip("\r\n"))[2] != "$082A"):
            raise ValueError("UVFactor editing requires verified main-array Flags/Flags2")
        if len(VariantStore._field_indices(lines, main_array, "UVFactor")) != 1:
            raise ValueError("UVFactor must occur exactly once directly on the main array")

    def inspect_parameters(self, project: str, variant: str,
                           include_saved_results: bool = False,
                           include_economics: bool = False,
                           include_topology: bool = False) -> dict:
        return variant_readback.inspect_parameters(
            self, project, variant,
            include_saved_results=include_saved_results,
            include_economics=include_economics,
            include_topology=include_topology,
        )

    def update_parameters(self, project: str, variant: str, expected_sha256: str | None = None,
                          subarray_id: int = 1, orientation_id: int = 1,
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
                          iam_updates: dict | None = None,
                          ageing_enabled: bool | None = None,
                          system_unavailability_updates: dict | None = None,
                          bifacial_updates: dict | None = None,
                          spectral_updates: dict | None = None,
                          native_economics_updates: dict | None = None) -> dict:
        return variant_updates.update_parameters(
            self, project, variant, expected_sha256, subarray_id, orientation_id,
            subarray_updates=subarray_updates,
            orientation_updates=orientation_updates,
            generator_updates=generator_updates,
            dry_run=dry_run,
            power_sharing_enabled=power_sharing_enabled,
            independent_mppt_enabled=independent_mppt_enabled,
            main_array_updates=main_array_updates,
            variant_name=variant_name,
            add_utf8_bom=add_utf8_bom,
            soiling_updates=soiling_updates,
            grid_limit_updates=grid_limit_updates,
            auxiliary_updates=auxiliary_updates,
            seasonal_updates=seasonal_updates,
            horizon_updates=horizon_updates,
            inverter_temperature_updates=inverter_temperature_updates,
            power_factor_updates=power_factor_updates,
            p50_p90_updates=p50_p90_updates,
            power_controls_updates=power_controls_updates,
            string_mismatch_updates=string_mismatch_updates,
            ageing_updates=ageing_updates,
            iam_updates=iam_updates,
            ageing_enabled=ageing_enabled,
            system_unavailability_updates=system_unavailability_updates,
            bifacial_updates=bifacial_updates,
            spectral_updates=spectral_updates,
            native_economics_updates=native_economics_updates,
        )

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

    def _replace_checked(self, path: Path, data: bytes, candidate: bytes, *, prepare=None, verify=None):
        return fileio.replace_checked(path, data, candidate, self._read,
                                      "Variant changed while preparing the edit",
                                      prepare=prepare, verify=verify)

    @staticmethod
    def _rollback(error: BaseException, steps, recovery: str) -> None:
        failed = []
        for name, undo, *args in steps:
            try:
                undo(*args)
            except Exception as exc:
                failed.append(f"{name} ({type(exc).__name__})")
        if failed and isinstance(error, Exception):
            raise OSError(f"{type(error).__name__}: {error}; rollback incomplete for "
                          f"{', '.join(failed)}; {recovery}") from error

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
        except BaseException as exc:
            self._rollback(exc, [(target.name, target.unlink, True) for target in reversed(created)],
                           "delete those partial files before retrying")
            raise
        return {"project": project, "variants": [path.suffix[1:] for path, _ in snapshots[1:]],
                "sha256": hashlib.sha256(snapshots[0][1]).hexdigest(),
                "files": {path.name: hashlib.sha256(data).hexdigest() for path, data in snapshots}}

    def create_project(self, project: str, project_content: str, variants: dict[str, str]) -> dict:
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
        with self.lock:
            self._check_project(source_project)
            source_root = self._root()
            target_prj = self._path(new_project)
            sources = [self._path(source_project)]
            for path in list(source_root.iterdir()):
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
            fileio.publish_new(target, candidate)
            return {"project": project, "variant": new_variant.upper(),
                    "source_project": source_project, "source_variant": source_variant.upper(),
                    "sha256": hashlib.sha256(candidate).hexdigest()}

    def update(self, project: str, variant: str, updates: dict[str, str],
               expected_sha256: str | None = None, subarray_id: int = 1) -> dict:
        with self.lock:
            self._check_project(project)
            path = self._path(project, variant)
            data = self._read(path)
            self._check_hash(data, expected_sha256)
            candidate = self._replace(data, updates, subarray_id)
            backup = self._backup(path, data)
            self._replace_checked(path, data, candidate)
            return {"project": project, "variant": variant.upper(), "backup_name": backup,
                    "sha256": hashlib.sha256(candidate).hexdigest()}

    _subarray_branches = staticmethod(variant_topology.subarray_branches)
    _require_plane_branch_mode = staticmethod(variant_topology.require_plane_branch_mode)

    def validate_structure(self, project: str, variant: str) -> dict:
        return variant_topology.validate_structure(self, project, variant)

    def _clone_subarray_data(self, data, source_subarray_id, *, clone_orientation=False,
                             share_inverter=False, reserved_ids=()):
        return variant_topology.clone_subarray_data(
            self, data, source_subarray_id,
            clone_orientation=clone_orientation,
            share_inverter=share_inverter,
            reserved_ids=reserved_ids,
        )

    def clone_subarray(self, project: str, variant: str, source_subarray_id: int,
                       expected_sha256: str | None = None, clone_orientation: bool = False,
                       share_inverter: bool = False) -> dict:
        return variant_topology.clone_subarray(
            self, project, variant, source_subarray_id, expected_sha256,
            clone_orientation=clone_orientation, share_inverter=share_inverter,
        )

    def remove_subarray(self, project: str, variant: str, subarray_id: int,
                        expected_sha256: str | None = None, remove_orientation: bool = False,
                        shared_member: bool = False) -> dict:
        return variant_topology.remove_subarray(
            self, project, variant, subarray_id, expected_sha256,
            remove_orientation=remove_orientation, shared_member=shared_member,
        )

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
                staged[path] = fileio.stage(path, data)
            expected = {path.name: hashlib.sha256(data).hexdigest()
                        for path, data in originals.items()}
            if self.inspect_project(project)["files"] != expected:
                raise ValueError("Project files changed while preparing the transaction")
            for path, temporary in staged.items():
                os.replace(temporary, path)
                written.append(path)
        except BaseException as exc:
            self._rollback(exc, [(path.name, self._replace_checked, path, candidates[path], originals[path])
                                 for path in reversed(written)],
                           f"restore the originals from backup transaction {transaction_id}")
            raise
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)
        return {"backups": backups, "transaction_id": transaction_id,
                "transaction_manifest": manifest_name,
                "files": {path.name: hashlib.sha256(data).hexdigest()
                          for path, data in candidates.items()}}

    def update_project_parameters(self, project: str, expected_files: dict[str, str] | None = None,
                                  project_name: str | None = None, dry_run: bool = False,
                                  add_utf8_bom: bool = False) -> dict:
        if project_name is None:
            raise ValueError("project_name is required")
        self._validate_display_name(project_name, "project_name")
        if not isinstance(dry_run, bool) or not isinstance(add_utf8_bom, bool):
            raise ValueError("dry_run and add_utf8_bom must be booleans")
        with self.lock:
            inventory = self.inspect_project(project)
            if expected_files is not None and inventory["files"] != expected_files:
                raise ValueError("Project files changed since inspection")
            files_to_read = inventory["files"] if expected_files is None else expected_files
            originals = {}
            for name, digest in files_to_read.items():
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
                               expected_files: dict[str, str] | None = None,
                               *, allow_synthetic_met: bool = False) -> dict:
        with self.lock:
            inventory = self.inspect_project(project)
            if expected_files is not None and inventory["files"] != expected_files:
                raise ValueError("Project files changed since inspection or has no variants")
            files_to_read = inventory["files"] if expected_files is None else expected_files
            if len(files_to_read) < 2:
                raise ValueError("Project files changed since inspection or has no variants")
            site_ref = self._workspace_asset("Sites", site_name)
            if allow_synthetic_met and not met_name:
                if not site_ref["workspace_file"]:
                    raise ValueError("SIT must be an existing workspace file")
                effective_met_name = f"{Path(site_name).stem}_Synthetic.MET"
                met_exists = False
            else:
                met_ref = self._workspace_asset("Meteo", met_name)
                if not site_ref["workspace_file"] or not met_ref["workspace_file"]:
                    raise ValueError("SIT and MET must be existing workspace files")
                effective_met_name = met_name
                met_exists = True
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
            from pvsyst_auto.project.synthesize import _is_pvmeteo_file, load_meteo_header

            met_path = self.workspace / "Meteo" / effective_met_name
            site_body = [line.strip() for line in site_lines[1:-1] if line.strip()]
            meteo_block_lines = (
                load_meteo_header(met_path, site_identity, site_body)[1]
                if met_exists and _is_pvmeteo_file(met_path) else None
            )
            originals = {}
            candidates = {}
            for name, digest in files_to_read.items():
                path = self._path(project) if name == project else self._path(project, name[len(project[:-4]) + 1:])
                data = self._read(path)
                self._check_hash(data, digest)
                text = data.decode("utf-8-sig")
                lines = text.splitlines(keepends=True)
                if name == project:
                    self._replace_site_block(lines, "SitePrj", site_lines)
                    self._replace_unique_field(lines, "MeteoFileName", effective_met_name, (0, len(lines)))
                else:
                    self._text(data)
                    self._replace_site_block(lines, "SiteSimul", site_lines)
                    start, end = self._section_bounds(lines, "PVObject_MeteoSimul=pvMeteo",
                                                      "End of PVObject pvMeteo")
                    child = next((i for i in range(start + 1, end)
                                  if lines[i].strip().startswith("PVObject_")), end)
                    self._replace_unique_field(lines, "NomF", effective_met_name, (start + 1, child))
                    self._replace_unique_field(lines, "SiteM", site_identity, (start + 1, child))
                    if meteo_block_lines is not None:
                        self._section_bounds(lines, "PVObject_SiteMet=pvSite", "End of PVObject pvSite")
                        indent = lines[start][:len(lines[start]) - len(lines[start].lstrip(" \t"))]
                        eol = "\r\n" if "\r\n" in lines[start] else "\n"
                        lines[start:end + 1] = [
                            eol if not raw_ln else (indent + (raw_ln[2:] if raw_ln.startswith("  ") else raw_ln) + eol)
                            for raw_ln in meteo_block_lines
                        ]
                    else:
                        self._replace_site_block(lines, "SiteMet", site_lines)
                    if "".join(lines) != text:
                        from pvsyst_auto.native.headless import strip_result_cache

                        lines, _ = strip_result_cache(lines)
                candidate = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")
                if name == project:
                    self._project_text(candidate)
                else:
                    self._sections(self._text(candidate)[1])
                originals[path] = data
                candidates[path] = candidate
            transaction = self._commit_project_files(project, originals, candidates)
            return {"project": project, "site": site_name, "meteo": met_name, **transaction}

    def restore_project_sources(self, project: str, backups: dict[str, str],
                                expected_files: dict[str, str] | None = None,
                                confirm: bool = False) -> dict:
        if not confirm:
            raise ValueError("Restore requires confirm=true")
        with self.lock:
            inventory = self.inspect_project(project)["files"]
            if (expected_files is not None and inventory != expected_files) or set(backups) != set(inventory):
                raise ValueError("Project files changed since inspection or backup set is incomplete")
            files_to_read = inventory if expected_files is None else expected_files
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
            for name, digest in files_to_read.items():
                path = self._path(project) if name == project else self._path(project, name[len(project[:-4]) + 1:])
                data = self._read(path)
                self._check_hash(data, digest)
                backup_name = backups[name]
                snapshot = self._backup_folder() / backup_name
                if snapshot.is_symlink() or snapshot.resolve().parent != self._backup_folder():
                    raise ValueError("Linked project backup is not supported")
                previous = self._read(snapshot)
                if hashlib.sha256(previous).hexdigest() != manifest["original_files"][name]:
                    raise ValueError(f"Project backup integrity check failed: {name}")
                if name == project:
                    try:
                        self._project_text(previous)
                    except ValueError as exc:
                        raise ValueError("Archived project snapshot is malformed") from exc
                else:
                    _, lines = self._text(previous)
                    self._sections(lines)
                originals[path] = data
                candidates[path] = previous
            return {"project": project, **self._commit_project_files(project, originals, candidates)}


    def restore(self, project: str, variant: str, backup_name: str,
                expected_sha256: str | None = None, confirm: bool = False) -> dict:
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
            self._replace_checked(path, data, previous_data)
            return {"project": project, "variant": variant.upper(), "backup_name": previous_backup,
                    "restored_from": backup_name, "sha256": hashlib.sha256(previous_data).hexdigest()}
