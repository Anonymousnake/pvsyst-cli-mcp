"""Pure source-snapshot, scene-source binding, and variant verification helpers (no Win32 GUI)."""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from pvsyst_auto.components.editor import objects
from pvsyst_auto.scene.author import (
    _num,
    check_tables,
    house_profile,
    obstacle_profiles,
    resolve_source_geometry,
    source_profile,
    table_profiles,
)
from pvsyst_auto.scene.mapping import (
    verify_grouped_hardware,
    verify_house_geometry,
    verify_module_layout,
    verify_pv_geometry,
    verify_tree_obstacles,
)


def find_running_pvsyst(exe: Path) -> list[int]:
    """Return PIDs of running PVsyst.exe matching `exe`, without importing GUI user32 wrappers."""
    if sys.platform != "win32":
        return []
    import ctypes.wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class Entry(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                    ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                    ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                    ("name", w.WCHAR * 260)]

    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(Entry)]
    kernel.Process32FirstW.restype = w.BOOL
    kernel.Process32NextW.restype = w.BOOL
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = w.BOOL
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = w.BOOL

    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise RuntimeError("Native process inventory is unavailable")
    found = []
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        present = kernel.Process32FirstW(handle, ctypes.byref(entry))
        if not present:
            raise RuntimeError("Native process inventory could not enumerate its first entry")
        while present:
            if entry.name.casefold() == exe.name.casefold():
                proc = kernel.OpenProcess(0x1000, False, entry.pid)
                path_str = ""
                if proc:
                    try:
                        size = w.DWORD(1024)
                        buf = ctypes.create_unicode_buffer(size.value)
                        if kernel.QueryFullProcessImageNameW(proc, 0, buf, ctypes.byref(size)):
                            path_str = buf.value
                    finally:
                        kernel.CloseHandle(proc)
                if not path_str:
                    raise RuntimeError("A same-name native process has an unreadable executable path")
                if Path(path_str).resolve() != exe:
                    raise RuntimeError("Another PVsyst installation is running; all sessions are left untouched")
                found.append(int(entry.pid))
            present = kernel.Process32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:
            raise RuntimeError("Native process inventory ended unexpectedly")
    finally:
        kernel.CloseHandle(handle)
    return found


def _source_snapshot(store, components, project, variant, module_geometry, *, allow_groups=False, layout=None):
    """Validate the source variant read-only, reusing the production helpers."""
    with store.lock:
        store._check_project(project)
        path = store._path(project, variant)
        original = store._read(path)
        _, lines = store._text(original)
        arrays, system = store._sections(lines)
        source = source_profile(store, lines, arrays, system, allow_groups=allow_groups)
        modules, pan_snapshots = resolve_source_geometry(components, source, module_geometry, layout)
    return {"path": path, "bytes": original, "source": source, "modules": modules,
            "pan_snapshot": (pan_snapshots[0] if len(pan_snapshots) == 1 else None),
            "pan_snapshots": pan_snapshots}


def _prepare_source(store, components, scene, *, allow_groups=False, error_cls=ValueError):
    """Bind lossless scene geometry to the current source, without driving the GUI."""
    args = scene["create_arguments"]
    snap = _source_snapshot(store, components, args["project"], args["variant"],
                            args["module_geometry"], allow_groups=allow_groups, layout=args["layout"])
    recorded, current = scene["source"], snap["source"]
    if recorded["pan"].casefold() != current["pan_name"].casefold():
        raise error_cls("The scene PAN reference differs from the current source variant")
    if recorded["module_total"] != current["module_total"]:
        raise error_cls("The scene module total differs from the current source variant")
    if (_num(recorded["tilt_deg"]) != _num(current["tilt_deg"])
            or _num(recorded["collada_azimuth_deg"]) != _num(current["azimuth_deg"])
            or any(_num(recorded["site"][key]) != _num(current["site"][key])
                   for key in ("latitude", "longitude", "altitude"))):
        raise error_cls("The scene PV plane or site differs from the current source variant")
    declared_groups = recorded.get("orientations", [])
    current_groups = current.get("orientations", [])
    if bool(declared_groups) != bool(current_groups):
        raise error_cls("The scene orientation structure differs from the current source variant")
    if declared_groups:
        actual_groups = recorded.get("orientation_geometry", [])
        if ([item["id"] for item in declared_groups] != [item["id"] for item in current_groups]
                or [item["id"] for item in actual_groups] != [item["id"] for item in current_groups]):
            raise error_cls("The scene must retain each current source orientation identity")
        for declared, actual, plane in zip(declared_groups, actual_groups, current_groups):
            if (declared["module_total"] != plane["module_total"]
                    or actual["module_total"] != plane["module_total"]
                    or _num(actual["tilt_deg"]) != _num(plane["tilt_deg"])
                    or _num(actual["collada_azimuth_deg"]) != _num(plane["azimuth_deg"])):
                raise error_cls("Scene orientation %d differs from the current source plane" % plane["id"])
    pocket = house_profile(args["house"])
    existing_names = ([m["name"] for m in pocket["meshes"]] if pocket["kind"] == "mesh" else ["House"])
    obstacles = obstacle_profiles(args.get("obstacles"), existing_names=existing_names)
    tables = table_profiles(args["layout"], pocket, snap["modules"], current)
    check_tables(pocket, tables)
    if len(tables) != int(scene["geometry"]["pv_table_count"]):
        raise error_cls("Recipe table count disagrees with the scene readback")
    return {"snapshot": snap, "house": pocket, "obstacles": obstacles, "table_profiles": tables}


def _verify_new(path, source_bytes, expect_parts, expect_tables, expect_modules, *, scene_context=None,
                 shading_mode="linear", error_cls=ValueError,
                 obstacle_gen_count=0, obstacle_tree_count=0):
    def parsed(raw):
        return objects(raw.decode("utf-8-sig"))
    target_bytes = Path(path).read_bytes()
    blocks = parsed(target_bytes)
    layout = {}
    if shading_mode == "module_layout":
        try:
            layout["module_layout"] = verify_module_layout(blocks, target_bytes.decode("utf-8-sig").splitlines(True))
        except (ValueError, KeyError) as exc:
            raise error_cls(str(exc)) from exc
    tables = [block for block in blocks if block.object_type == "pvShdArrayTable"]
    generic = [block for block in blocks if block.object_type == "pvShdGenObject"]
    elems = [block for block in blocks if block.object_type == "pvShdElemObject"]
    modules = sum(int(block.one("NModInHeight")[1]) * int(block.one("NModInWidth")[1]) for block in tables)
    expected_generic = expect_parts + obstacle_gen_count
    if len(generic) != expected_generic or len(tables) != expect_tables or len(elems) != obstacle_tree_count:
        raise error_cls(
            "New variant holds %d generic objects, %d elementary objects and %d PV tables, expected %d, %d and %d"
            % (len(generic), len(elems), len(tables), expected_generic, obstacle_tree_count, expect_tables))
    if modules != expect_modules:
        raise error_cls("New variant module total is %d, expected %d" % (modules, expect_modules))
    if scene_context is not None and scene_context["source"].get("orientations"):
        source_text, target_text = (raw.decode("utf-8-sig") for raw in (source_bytes, target_bytes))
        obs_list = scene_context.get("obstacles") or []
        obs_meshes = [o for o in obs_list if o["kind"] != "tree"]
        obs_trees = [o for o in obs_list if o["kind"] == "tree"]
        try:
            hardware = verify_grouped_hardware(parsed(source_bytes), blocks, source_text.splitlines(True),
                                               target_text.splitlines(True), scene_context["source"])
            house_geometry = verify_house_geometry(blocks, target_text.splitlines(True), scene_context["house"],
                                                    scene_context["source"], extra_meshes=obs_meshes)
            pv_geometry = verify_pv_geometry(blocks, scene_context["tables"], scene_context["modules"],
                                             scene_context["source"])
            tree_geometry = verify_tree_obstacles(
                blocks, obs_trees, scene_context["source"],
                anchor_turn=360.0 - next(
                    item for item in scene_context["source"]["orientations"] if item["id"] == 1
                )["azimuth_deg"],
            )
        except ValueError as exc:
            raise error_cls(str(exc)) from exc
        return {"objects": len(generic) + len(elems) + len(tables), "house": len(generic),
                "trees": len(elems), "tables": len(tables),
                "modules": modules, **hardware, **house_geometry, **tree_geometry, **pv_geometry, **layout}
    if len([block for block in blocks if block.object_type == "pvOrient"]) != 1 or any(
            block.one("NoOrient")[1] != "1" for block in tables):
        raise error_cls("New variant does not reference a single orientation")
    if any(block.one("Source")[1] != "osPVCollada" for block in generic + tables):
        raise error_cls("New variant does not preserve the native PVCollada provenance")
    original = next(block for block in parsed(source_bytes) if block.object_type == "pvSubArray")
    target = next(block for block in blocks if block.object_type == "pvSubArray")
    for field in ("PVModule", "GInverter", "NModSerie", "NStringCh", "NStrOrient1", "NInverter", "NInvMPPT"):
        if original.one(field)[1] != target.one(field)[1]:
            raise error_cls("Saved variant hardware differs from the source: " + field)
    return {"objects": len(generic) + len(elems) + len(tables), "house": len(generic),
            "trees": len(elems), "tables": len(tables),
            "modules": modules, "source_hardware_preserved": True}
