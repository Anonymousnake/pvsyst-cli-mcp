"""Subarray branch topology checks, cloning, removal, and variant structure validation."""
from __future__ import annotations

import hashlib
import re

from pvsyst_auto.components.editor import objects, replace_value, root_block
from pvsyst_auto.components.store import FIELD
from pvsyst_auto.project.paths import VARIANT_ID_RE
from pvsyst_auto.project.circuit_core import (
    MAX_COUNT,
    _encode_candidate,
    _need,
    _require_single_orient_strings,
    _walk,
    circuit_nodes,
    inventory,
    positive_integer,
    require_ordinary_mppt,
)
from pvsyst_auto.validation import NATIVE_TEXT_OR_SEED


def subarray_branches(lines, arrays, identifier):
    start, end = arrays[identifier]
    root = next(b for b in objects("".join(lines[start:end + 1])) if not b.path)
    root.one("NoOrientation")
    if root.one("SubArrayId")[1] != str(identifier):
        raise ValueError("Subarray ID does not match its owning object")
    name = root.one("Comment")[1]
    for other_id, (first, last) in arrays.items():
        other = root if other_id == identifier else next(b for b in objects("".join(lines[first:last + 1])) if not b.path)
        if "PowerSharingInverterId" in other.fields and other.one("PowerSharingInverterId")[1] != "-1":
            raise ValueError("Shared inverter power is not supported for subarray editing")
    matches, owned = [], {start + root.one("SubArrayId")[0]}
    for node in (item for tree in circuit_nodes(lines) for item in _walk(tree)):
        if node.kind != "InverterNode":
            continue
        members = list(_walk(node))
        ids = [m.one("SubArrayId") for m in members]
        if str(identifier) not in ids:
            continue
        if any(v != str(identifier) for v in ids):
            raise ValueError("Subarray must own complete inverter branches without shared allocation")
        if any(m.one("SubArrayName") != name for m in members):
            raise ValueError("Circuit branch names do not match the subarray")
        owned.update(idx for m in members for idx in m.field_lines["SubArrayId"])
        matches.append((node.line - 1, node.end_line - 1, members))
    occurrences = {i for i, ln in enumerate(lines) if (f := FIELD.fullmatch(ln.rstrip("\r\n"))) and f[1] == "SubArrayId" and f[2] == str(identifier)}
    if not matches or not occurrences.issubset(owned):
        raise ValueError("Subarray ID is referenced outside its complete inverter branches")
    checked = inventory(lines, arrays)
    if any(r["id"] == identifier and r["explicit_mppt_inputs_match_declared"] is False for r in checked["subarrays"]):
        raise ValueError("Independent MPPT layout does not match its declared input count")
    if len(matches) > 1 and checked["counts_valid"] is not True:
        raise ValueError("Grouped subarray editing requires fully checked consistent grid counts")
    return matches, root

def require_plane_branch_mode(root, branches):
    flags = root.one("Flags")[1] if "Flags" in root.fields else ""
    if re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) and int(flags[1:], 16) & 0x400:
        if ("Version" not in root.fields or root.one("Version")[1] not in NATIVE_TEXT_OR_SEED
                or "PowerSharingInverterId" not in root.fields or root.one("PowerSharingInverterId")[1] != "-1"
                or any(not members[0].children or any(c.kind != "MPPTNode" for c in members[0].children) for _, _, members in branches)):
            raise ValueError("Plane editing requires verified exclusive explicit MPPT allocation")
    else:
        require_ordinary_mppt(root)
        if any(m.kind == "MPPTNode" for _, _, members in branches for m in members):
            raise ValueError("Plane editing requires ordinary branches without explicit MPPT")
    if any(set(m.fields) & {"NoOrientation", "NoOrient", "OrientationId"} for _, _, members in branches for m in members):
        raise ValueError("Circuit-level orientation references outside owned records require separate allocation handling")

def _validate_clone_subarray_args(source_subarray_id: int, clone_orientation: bool, share_inverter: bool) -> None:
    if not isinstance(source_subarray_id, int) or isinstance(source_subarray_id, bool):
        raise ValueError("source_subarray_id must be an integer")
    if not isinstance(clone_orientation, bool) or not isinstance(share_inverter, bool):
        raise ValueError("clone_orientation and share_inverter must be booleans")
    if share_inverter and clone_orientation:
        raise ValueError("Shared-parent cloning requires the existing common orientation")

def clone_subarray_data(store, data, source_subarray_id, *, clone_orientation=False, share_inverter=False, reserved_ids=()):
    """Build one cloned subarray in memory without reading or writing workspace files."""
    from pvsyst_auto.native.headless import strip_result_cache
    _validate_clone_subarray_args(source_subarray_id, clone_orientation, share_inverter)
    _, lines = store._text(data)
    arrays, system = store._sections(lines)
    if source_subarray_id not in arrays or system is None:
        raise ValueError("Source subarray and grid system are required")
    st = store._field_indices(lines, system, "SystemType")
    if len(st) != 1 or FIELD.fullmatch(lines[st[0]].rstrip("\r\n"))[2] != "Grid":
        raise ValueError("Only a grid system is supported")
    store._require_no_geometry(lines, require_shading_section=True)
    bounds = arrays[source_subarray_id]
    orient_f = store._field_indices(lines, bounds, "NoOrientation")
    if len(orient_f) != 1 or not (orientation_id := FIELD.fullmatch(lines[orient_f[0]].rstrip("\r\n"))[2]).isdigit():
        raise ValueError("Source subarray requires one orientation" if len(orient_f) != 1 else "Source orientation is invalid")
    orientation = store._orientation(lines, int(orientation_id))
    ft = store._field_indices(lines, orientation, "FieldType")
    if len(ft) != 1 or FIELD.fullmatch(lines[ft[0]].rstrip("\r\n"))[2] != "FixedPlane":
        raise ValueError("Only a fixed-plane orientation is supported")
    matches, root = subarray_branches(lines, arrays, source_subarray_id)
    if share_inverter:
        flags = root.one("Flags")[1]
        if (root.one("Version")[1] not in NATIVE_TEXT_OR_SEED or not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags)
                or not int(flags[1:], 16) & 0x400 or root.one("NInverter")[1] != "1" or root.one("NInvMPPT")[1] != "1"
                or root.one("PowerSharingInverterId")[1] != "-1" or inventory(lines, arrays)["counts_valid"] is not True or len(matches) != 1):
            raise ValueError("Shared-parent cloning requires one verified independent unshared input/inverter")
        inv = matches[0][2][0]
        if (positive_integer(inv.one("NElements", "1"), "inverter multiplicity") != 1 or len(inv.children) != 1
                or inv.children[0].kind != "MPPTNode" or positive_integer(inv.children[0].one("NElements", "1"), "MPPT multiplicity") != 1):
            raise ValueError("Shared-parent cloning requires one direct explicit MPPT input")
        _require_single_orient_strings(root, "Shared-parent cloning requires all strings in one orientation")
    new_id = max(set(arrays) | set(reserved_ids)) + 1
    if new_id > MAX_COUNT:
        raise ValueError("No available subarray ID")
    new_name, new_array = f"Sub-array #{new_id}", lines[bounds[0]:bounds[1] + 1].copy()
    for key, val in (("SubArrayId", str(new_id)), ("Comment", new_name)):
        new_array[root.one(key)[0]] = replace_value(new_array[root.one(key)[0]], val)
    new_orientation_id, insertions = int(orientation_id), [(bounds[1] + 1, new_array)]
    if clone_orientation:
        require_plane_branch_mode(root, matches)
        if inventory(lines, arrays)["counts_valid"] is not True:
            raise ValueError("Independent orientation cloning requires complete consistent grid counts")
        _require_single_orient_strings(root, "Independent orientation cloning requires all strings in one orientation")
        store._ordinary_fixed_plane(lines, orientation)
        header, groups = store._orientation_list(lines)
        if [gid for _, _, gid in groups] != list(range(1, len(groups) + 1)):
            raise ValueError("Independent orientation cloning requires sequential ordered IDs")
        new_orientation_id = len(groups) + 1
        if new_orientation_id > MAX_COUNT:
            raise ValueError("No available orientation ID")
        new_plane = lines[orientation[0]:orientation[1] + 1].copy()
        pidx = store._field_indices(lines, orientation, "NoOrient")[0] - orientation[0]
        new_plane[pidx] = replace_value(new_plane[pidx], str(new_orientation_id))
        aidx = root.one("NoOrientation")[0]
        new_array[aidx] = replace_value(new_array[aidx], str(new_orientation_id))
        lines[header] = re.sub(r"(?<==)[0-9]+", str(new_orientation_id), lines[header], count=1)
        insertions.append((groups[-1][1] + 1, new_plane))
    for start, end, members in matches:
        if share_inverter:
            inv, port = members[0], members[0].children[0]
            start, end, members = port.line - 1, port.end_line - 1, members[1:]
            sid_ln, snm_ln = inv.field_lines["SubArrayId"][0], inv.field_lines["SubArrayName"][0]
            lines[sid_ln] = replace_value(lines[sid_ln], "0")
            lines[snm_ln] = replace_value(lines[snm_ln], root.one("Comment")[1] + "; " + new_name)
        branch = lines[start:end + 1].copy()
        for member in members:
            for key, val in (("SubArrayId", str(new_id)), ("SubArrayName", new_name)):
                idx = member.field_lines[key][0] - start
                branch[idx] = replace_value(branch[idx], val)
        insertions.append((end + 1, branch))
    for offset, block in sorted(insertions, key=lambda item: item[0], reverse=True):
        lines[offset:offset] = block
    lines, _ = strip_result_cache(lines)
    candidate = _encode_candidate(data, lines)
    checked_arrays, _ = store._sections(checked := store._text(candidate)[1])
    if new_id not in checked_arrays or len(checked_arrays) != len(arrays) + 1:
        raise ValueError("Subarray structure validation failed")
    if share_inverter:
        inv_res = inventory(checked, checked_arrays)
        groups = [g for g in inv_res["inverter_groups"] if g.get("subarray_ids") == [source_subarray_id, new_id]]
        if inv_res["counts_valid"] is not True or len(groups) != 1:
            raise ValueError("Shared-parent clone failed complete allocation validation")
    elif len(subarray_branches(checked, checked_arrays, new_id)[0]) != len(matches):
        raise ValueError("Subarray inverter groups were not completely copied")
    if clone_orientation:
        store._orientation_list(checked)
        store._orientation(checked, new_orientation_id)
    return candidate, {"subarray_id": new_id, "orientation_id": new_orientation_id, "orientation_cloned": clone_orientation,
                       "inverter_group_count": len(matches), "shared_parent_created": share_inverter}

def clone_subarray(store, project: str, variant: str, source_subarray_id: int, expected_sha256: str | None = None,
                   clone_orientation: bool = False, share_inverter: bool = False) -> dict:
    """Duplicate a grid subarray with all its exclusively owned inverter groups."""
    _validate_clone_subarray_args(source_subarray_id, clone_orientation, share_inverter)
    with store.lock:
        store._check_project(project)
        path = store._path(project, variant)
        data = store._read(path)
        store._check_hash(data, expected_sha256)
        reserved_ids = set()
        for item in store._root().iterdir():
            if item.stem.casefold() == project[:-4].casefold() and item.suffix.startswith(".") and VARIANT_ID_RE.fullmatch(item.suffix[1:]):
                reserved_ids.update(store._sections(store._text(store._read(store._path(project, item.suffix[1:])))[1])[0])
        candidate, details = clone_subarray_data(store, data, source_subarray_id, clone_orientation=clone_orientation,
                                                 share_inverter=share_inverter, reserved_ids=reserved_ids)
        backup = store._replace_checked(path, data, candidate, prepare=lambda: store._backup(path, data))
        return {"project": project, "variant": variant.upper(), **details, "backup_name": backup, "sha256": hashlib.sha256(candidate).hexdigest()}

def remove_subarray(store, project: str, variant: str, subarray_id: int, expected_sha256: str | None = None,
                    remove_orientation: bool = False, shared_member: bool = False) -> dict:
    """Remove a subarray and its owned groups, retaining an orientation still in use."""
    from pvsyst_auto.native.headless import strip_result_cache
    _need(not isinstance(subarray_id, int) or isinstance(subarray_id, bool), "subarray_id must be an integer")
    _need(not isinstance(remove_orientation, bool) or not isinstance(shared_member, bool), "remove_orientation and shared_member must be booleans")
    _need(shared_member and remove_orientation, "Shared-member deletion preserves the common orientation")
    with store.lock:
        store._check_project(project)
        path = store._path(project, variant)
        data = store._read(path)
        store._check_hash(data, expected_sha256)
        _, lines = store._text(data)
        arrays, system = store._sections(lines)
        _need(subarray_id not in arrays or len(arrays) < 2 or system is None, "A target and another remaining subarray are required")
        kinds = store._field_indices(lines, system, "SystemType")
        _need(len(kinds) != 1 or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid", "Only grid subarrays are supported")
        store._require_no_geometry(lines, require_shading_section=True)
        bounds = arrays[subarray_id]
        no_orient = store._field_indices(lines, bounds, "NoOrientation")
        _need(len(no_orient) != 1, "Subarray needs one orientation")
        orientation = FIELD.fullmatch(lines[no_orient[0]].rstrip("\r\n"))[2]
        _need(not orientation.isdigit() or not 1 <= int(orientation) <= MAX_COUNT, "Orientation ID must be a positive 32-bit integer")
        shared = any(other != subarray_id and any((raw := FIELD.fullmatch(lines[i].rstrip("\r\n"))[2]).isdigit() and int(raw) == int(orientation)
                                                 for i in store._field_indices(lines, ob, "NoOrientation"))
                     for other, ob in arrays.items())
        _need(shared and remove_orientation, "Orientation is still referenced by another subarray")
        _need(not shared and not remove_orientation, "Orientation must remain in use by another subarray")
        remaining_member = None
        if shared_member:
            checked = inventory(lines, arrays)
            groups = [g for g in checked["inverter_groups"] if subarray_id in g.get("subarray_ids", [])]
            _need(checked["counts_valid"] is not True or len(groups) != 1 or len(groups[0]["subarray_ids"]) != 2
                  or groups[0]["power_sharing_id"] != -1 or groups[0]["inverters"] != 1,
                  "Shared-member deletion requires one complete disabled two-member parent")
            member_ids, roots = groups[0]["subarray_ids"], {}
            for ident, (first, last) in arrays.items():
                peer = next(b for b in objects("".join(lines[first:last + 1])) if not b.path)
                _need("PowerSharingInverterId" in peer.fields and peer.one("PowerSharingInverterId")[1] != "-1",
                      "Disable all power sharing before shared-member deletion")
                if ident in member_ids:
                    row = next(r for r in checked["subarrays"] if r["id"] == ident)
                    _need(peer.one("NInverter")[1] != "1" or peer.one("NInvMPPT")[1] != "1" or peer.one("NoOrientation")[1] != orientation
                          or row["circuit"]["inverters"] != 1 or len(row["shared_inverter_lines"]) != 1,
                          "Shared-member deletion requires single-input members in a common plane")
                    _require_single_orient_strings(peer, "Shared-member deletion requires single-input members in a common plane")
                    roots[ident] = peer
            types = store._field_indices(lines, store._orientation(lines, int(orientation)), "FieldType")
            _need(len(types) != 1 or FIELD.fullmatch(lines[types[0]].rstrip("\r\n"))[2] != "FixedPlane", "Shared-member deletion requires a fixed plane")
            parent = next(n for tree in circuit_nodes(lines) for n in _walk(tree) if n.line == groups[0]["line"])
            _need(parent.one("SubArrayId") != "0", "Shared-member deletion requires explicit parent owner 0")
            port = next((n for n in parent.children if n.one("SubArrayId") == str(subarray_id)), None)
            _need(port is None, "Shared-member deletion requires a direct port with the exact SubArrayId")
            owned = {bounds[0] + roots[subarray_id].one("SubArrayId")[0]} | {idx for n in _walk(port) for idx in n.field_lines["SubArrayId"]}
            refs = {i for i, ln in enumerate(lines) if (f := FIELD.fullmatch(ln.rstrip("\r\n"))) and f[1] == "SubArrayId" and f[2] == str(subarray_id)}
            _need(not refs.issubset(owned), "Shared member has references outside its owned subtree")
            remaining_member = next(i for i in member_ids if i != subarray_id)
            for key, val in (("SubArrayId", str(remaining_member)), ("SubArrayName", roots[remaining_member].one("Comment")[1])):
                idx = parent.field_lines[key][0]
                lines[idx] = replace_value(lines[idx], val)
            root, matches = roots[subarray_id], [(port.line - 1, port.end_line - 1, list(_walk(port)))]
        else:
            matches, root = subarray_branches(lines, arrays, subarray_id)
        deletions, orientation_id_map = [bounds] + [(s, e) for s, e, _ in matches], {}
        if remove_orientation:
            _need(inventory(lines, arrays)["counts_valid"] is not True, "Orientation removal requires complete consistent grid counts")
            header, groups = store._orientation_list(lines)
            _need(len(groups) < 2, "Another orientation must remain")
            _need([gid for _, _, gid in groups] != list(range(1, len(groups) + 1)), "Orientation removal requires sequential ordered IDs")
            plane = store._orientation(lines, int(orientation))
            fields = store._field_indices(lines, plane, "FieldType")
            _need(len(fields) != 1 or FIELD.fullmatch(lines[fields[0]].rstrip("\r\n"))[2] != "FixedPlane", "Only a fixed-plane orientation can be removed")
            _need([i for i, ln in enumerate(lines) if (f := FIELD.fullmatch(ln.rstrip("\r\n"))) and f[1] == "NoOrientation"
                   and f[2].isdigit() and int(f[2]) == int(orientation)] != [bounds[0] + root.one("NoOrientation")[0]],
                  "Orientation has references outside its owning subarray")
            owned_refs = set()
            for first, last, gid in groups:
                store._ordinary_fixed_plane(lines, (first, last))
                owned_refs.add(store._field_indices(lines, (first, last), "NoOrient")[0])
                if gid > int(orientation):
                    orientation_id_map[str(gid)] = gid - 1
            for ident, (first, last) in arrays.items():
                array_root = root if ident == subarray_id else next(b for b in objects("".join(lines[first:last + 1])) if not b.path)
                require_plane_branch_mode(array_root, subarray_branches(lines, arrays, ident)[0])
                _require_single_orient_strings(array_root, "Orientation removal requires all strings in one orientation")
                owned_refs.add(first + array_root.one("NoOrientation")[0])
            for idx, ln in enumerate(lines):
                if (f := FIELD.fullmatch(ln.rstrip("\r\n"))) and (f[1].startswith("NoOrient") or f[1] == "OrientationId") and idx not in owned_refs:
                    raise ValueError("Orientation removal has references outside owned records")
            for idx in owned_refs:
                if str(int(raw := FIELD.fullmatch(lines[idx].rstrip("\r\n"))[2])) in orientation_id_map:
                    lines[idx] = replace_value(lines[idx], str(orientation_id_map[str(int(raw))]))
            lines[header] = re.sub(r"(?<==)[0-9]+", str(len(groups) - 1), lines[header], count=1)
            deletions.append(plane)
        for first, last in sorted(deletions, reverse=True):
            del lines[first:last + 1]
        lines, _ = strip_result_cache(lines)
        candidate = _encode_candidate(data, lines)
        checked_arrays, _ = store._sections(checked := store._text(candidate)[1])
        _need(subarray_id in checked_arrays or len(checked_arrays) != len(arrays) - 1, "Subarray removal failed validation")
        if shared_member:
            subarray_branches(checked, checked_arrays, remaining_member)
        _need((len(matches) > 1 or remove_orientation or shared_member) and inventory(checked, checked_arrays)["counts_valid"] is not True,
              "Remaining grouped grid counts failed validation")
        if remove_orientation:
            store._orientation_list(checked)
        backup = store._replace_checked(path, data, candidate, prepare=lambda: store._backup(path, data))
        return {"project": project, "variant": variant.upper(), "removed_subarray_id": subarray_id,
                "removed_orientation_id": int(orientation) if remove_orientation else None,
                "orientation_id_map": orientation_id_map, "remaining_member_id": remaining_member,
                "inverter_group_count": len(matches), "backup_name": backup, "sha256": hashlib.sha256(candidate).hexdigest()}

def validate_structure_lines(store, lines: list[str]) -> dict:
    """Check cross-object references inside in-memory variant lines."""
    arrays, system = store._sections(lines)
    issues, warnings, orientation_ids = [], [], set()
    for index, line in enumerate(lines):
        if line.strip() not in ("PVObject_=pvOrient", "PVObject_=pvDomeOrient"):
            continue
        closing = "End of TOrientGroup" if line.strip() == "PVObject_=pvOrient" else "End of TDomeOrientGroup"
        if (end := next((i for i in range(index + 1, len(lines)) if lines[i].strip() == closing), None)) is None:
            issues.append("Unclosed orientation")
        elif len(identifiers := store._field_indices(lines, (index, end), "NoOrient")) != 1:
            issues.append("Orientation needs one NoOrient")
        elif not (raw := FIELD.fullmatch(lines[identifiers[0]].rstrip("\r\n"))[2]).isdigit() or int(raw) in orientation_ids:
            issues.append(f"Duplicate or invalid orientation ID: {raw}")
        else:
            orientation_ids.add(int(raw))
    list_headers, list_unchecked = [ln.strip() for ln in lines if ln.strip().startswith("Orientations, list of=")], []
    if list_headers:
        if any(not ln.endswith(" TOrientGroup") for ln in list_headers):
            list_unchecked.append("orientation list counts for unknown list types")
        else:
            try:
                store._orientation_list(lines)
            except ValueError as exc:
                issues.append(str(exc))
    for ident, bounds in arrays.items():
        try:
            root = root_block(lines, bounds)
            fl_raw = root.one("Flags")[1] if "Flags" in root.fields else ""
            f2_raw = root.one("Flags2")[1] if "Flags2" in root.fields else ""
            nstr_raw = root.one("NStringCh")[1] if "NStringCh" in root.fields else ""
            nso1_raw = root.one("NStrOrient1")[1] if "NStrOrient1" in root.fields else ""
            ps_raw = root.one("PowerSharingInverterId")[1] if "PowerSharingInverterId" in root.fields else None
        except ValueError as exc:
            issues.append(f"Subarray {ident}: {exc}")
            continue
        if len(indices := store._field_indices(lines, bounds, "NoOrientation")) != 1:
            issues.append(f"Subarray {ident} needs one NoOrientation")
            continue
        if not (raw := FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]).isdigit() or int(raw) not in orientation_ids:
            issues.append(f"Subarray {ident} references unknown orientation {raw}")
            continue
        fl_val = int(fl_raw[1:], 16) if re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", fl_raw) else None
        if indices2 := store._field_indices(lines, bounds, "NoOrientation2"):
            if len(indices2) != 1:
                issues.append(f"Subarray {ident} has repeated NoOrientation2")
            elif not (raw2 := FIELD.fullmatch(lines[indices2[0]].rstrip("\r\n"))[2]).isdigit() or int(raw2) not in orientation_ids or int(raw2) == int(raw):
                issues.append(f"Subarray {ident} references invalid NoOrientation2 {raw2}")
            if not nstr_raw.isdigit() or not nso1_raw.isdigit() or not 1 <= int(nso1_raw) < int(nstr_raw):
                issues.append(f"Subarray {ident} mixed NStrOrient1 must satisfy 1 <= NStrOrient1 < NStringCh")
            if fl_val is None or not (fl_val & 0x40):
                issues.append(f"Subarray {ident} requires mixed-orientation Flags bit $40 with NoOrientation2")
            if fl_val is not None and (fl_val & 0x100):
                issues.append(f"Subarray {ident} cannot set power-sharing Flags bit $100 with NoOrientation2")
            if fl_val is not None and (fl_val & 0x400):
                issues.append(f"Subarray {ident} cannot set independent-MPPT Flags bit $400 with NoOrientation2")
            f2_val = int(f2_raw[1:], 16) if re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", f2_raw) else None
            if f2_val is None or not (f2_val & 0x1):
                issues.append(f"Subarray {ident} requires Flags2 bit $01 with NoOrientation2")
            if ps_raw != "-1":
                issues.append(f"Subarray {ident} with NoOrientation2 requires explicit PowerSharingInverterId=-1")
        elif fl_val is not None and (fl_val & 0x40):
            issues.append(f"Subarray {ident} sets mixed-orientation flag without NoOrientation2")
    system_type, unchecked, circuit_res = None, list_unchecked, None
    if system is None:
        issues.append("Missing pvSystem section")
        unchecked.append("system circuit references")
    elif len(kinds := store._field_indices(lines, system, "SystemType")) != 1:
        issues.append("System needs exactly one SystemType")
        unchecked.append("system circuit references")
    else:
        if not (system_type := FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2]):
            issues.append("SystemType must not be empty")
        if system_type == "Grid":
            circuit_res = inventory(lines, arrays)
            issues.extend(circuit_res["issues"])
            unchecked.extend(circuit_res["unchecked_checks"])
        else:
            unchecked.append("system circuit references")
            warnings.append(f"Circuit checks are not implemented for SystemType={system_type!r}")
    if any("PVObject_ShdTable" in ln or "ListeObjets, list of=" in ln for ln in lines):
        warnings.append("Shading geometry and derived factor tables require simulation verification")
    return {"valid": False if issues else None if unchecked else True, "checks_complete": not unchecked,
            "unchecked_checks": unchecked, "system_type": system_type, "grid_circuit": circuit_res,
            "issues": issues, "warnings": warnings, "subarray_ids": sorted(arrays), "orientation_ids": sorted(orientation_ids)}


def validate_structure(store, project: str, variant: str) -> dict:
    """Check cross-object references inside a variant."""
    with store.lock:
        store._check_project(project)
        lines = store._text(data := store._read(store._path(project, variant)))[1]
        return {"project": project, "variant": variant.upper(), "sha256": hashlib.sha256(data).hexdigest(),
                **validate_structure_lines(store, lines)}
