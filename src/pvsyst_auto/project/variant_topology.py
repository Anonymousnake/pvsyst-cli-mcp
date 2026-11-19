"""Subarray topology validation, cloning, and deletion for PVsyst variants."""
from __future__ import annotations

import hashlib
import re

from pvsyst_auto.components.editor import objects, replace_value
from pvsyst_auto.components.store import FIELD
from pvsyst_auto.native.headless import strip_result_cache
from pvsyst_auto.project.circuit import circuit_nodes, inventory as grid_inventory, positive_integer, require_ordinary_mppt
from pvsyst_auto.versions import NATIVE_TEXT_OR_SEED


def subarray_branches(lines, arrays, identifier):
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


def require_plane_branch_mode(root, branches):
    flags = root.one("Flags")[1] if "Flags" in root.fields else ""
    if re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) and int(flags[1:], 16) & 0x400:
        if ("Version" not in root.fields or root.one("Version")[1] not in NATIVE_TEXT_OR_SEED
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


def validate_structure(store, project: str, variant: str) -> dict:
    """Check cross-object references inside a variant."""
    with store.lock:
        store._check_project(project)
        data = store._read(store._path(project, variant))
        _, lines = store._text(data)
        arrays, system = store._sections(lines)
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
            identifiers = store._field_indices(lines, (index, end), "NoOrient")
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
                    store._orientation_list(lines)
                except ValueError as exc:
                    issues.append(str(exc))
        for identifier, bounds in arrays.items():
            indices = store._field_indices(lines, bounds, "NoOrientation")
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
            kinds = store._field_indices(lines, system, "SystemType")
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
        return {
            "project": project,
            "variant": variant.upper(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "valid": False if issues else None if unchecked else True,
            "checks_complete": not unchecked,
            "unchecked_checks": unchecked,
            "system_type": system_type,
            "grid_circuit": circuit,
            "issues": issues,
            "warnings": warnings,
            "subarray_ids": sorted(arrays),
            "orientation_ids": sorted(orientation_ids),
        }


def clone_subarray_data(store, data, source_subarray_id, *, clone_orientation=False,
                        share_inverter=False, reserved_ids=()):
    """Build one cloned subarray in memory without reading or writing workspace files."""
    if not isinstance(source_subarray_id, int) or isinstance(source_subarray_id, bool):
        raise ValueError("source_subarray_id must be an integer")
    if not isinstance(clone_orientation, bool) or not isinstance(share_inverter, bool):
        raise ValueError("clone_orientation and share_inverter must be booleans")
    if share_inverter and clone_orientation:
        raise ValueError("Shared-parent cloning requires the existing common orientation")
    _, lines = store._text(data)
    arrays, system = store._sections(lines)
    if source_subarray_id not in arrays or system is None:
        raise ValueError("Source subarray and grid system are required")
    system_type = store._field_indices(lines, system, "SystemType")
    if len(system_type) != 1 or FIELD.fullmatch(lines[system_type[0]].rstrip("\r\n"))[2] != "Grid":
        raise ValueError("Only a grid system is supported")
    store._require_no_geometry(lines, require_shading_section=True)
    bounds = arrays[source_subarray_id]
    orientation_fields = store._field_indices(lines, bounds, "NoOrientation")
    if len(orientation_fields) != 1:
        raise ValueError("Source subarray requires one orientation")
    orientation_id = FIELD.fullmatch(lines[orientation_fields[0]].rstrip("\r\n"))[2]
    if not orientation_id.isdigit():
        raise ValueError("Source orientation is invalid")
    orientation = store._orientation(lines, int(orientation_id))
    field_type = store._field_indices(lines, orientation, "FieldType")
    if len(field_type) != 1 or FIELD.fullmatch(lines[field_type[0]].rstrip("\r\n"))[2] != "FixedPlane":
        raise ValueError("Only a fixed-plane orientation is supported")
    matches, root = subarray_branches(lines, arrays, source_subarray_id)
    if share_inverter:
        flags = root.one("Flags")[1]
        if (root.one("Version")[1] not in NATIVE_TEXT_OR_SEED or not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags)
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
    current_ids = set(arrays) | set(reserved_ids)
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
        require_plane_branch_mode(root, matches)
        if grid_inventory(lines, arrays)["counts_valid"] is not True:
            raise ValueError("Independent orientation cloning requires complete consistent grid counts")
        if (root.one("NStrOrient1")[1] != root.one("NStringCh")[1] or any(
                key.startswith("NStrOrient") and key != "NStrOrient1" and root.one(key)[1] != "0"
                for key in root.fields)):
            raise ValueError("Independent orientation cloning requires all strings in one orientation")
        store._ordinary_fixed_plane(lines, orientation)
        header, groups = store._orientation_list(lines)
        if [identifier for _, _, identifier in groups] != list(range(1, len(groups) + 1)):
            raise ValueError("Independent orientation cloning requires sequential ordered IDs")
        new_orientation_id = len(groups) + 1
        if new_orientation_id > 2147483647:
            raise ValueError("No available orientation ID")
        new_plane = lines[orientation[0]:orientation[1] + 1].copy()
        index = store._field_indices(lines, orientation, "NoOrient")[0] - orientation[0]
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
    lines, _ = strip_result_cache(lines)
    candidate = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")
    _, checked = store._text(candidate)
    checked_arrays, _ = store._sections(checked)
    if new_id not in checked_arrays or len(checked_arrays) != len(arrays) + 1:
        raise ValueError("Subarray structure validation failed")
    if share_inverter:
        inventory = grid_inventory(checked, checked_arrays)
        groups = [group for group in inventory["inverter_groups"] if group.get("subarray_ids") == [source_subarray_id, new_id]]
        if inventory["counts_valid"] is not True or len(groups) != 1:
            raise ValueError("Shared-parent clone failed complete allocation validation")
    else:
        new_matches, _ = subarray_branches(checked, checked_arrays, new_id)
        if len(new_matches) != len(matches):
            raise ValueError("Subarray inverter groups were not completely copied")
    if clone_orientation:
        store._orientation_list(checked)
        store._orientation(checked, new_orientation_id)
    return candidate, {
        "subarray_id": new_id,
        "orientation_id": new_orientation_id,
        "orientation_cloned": clone_orientation,
        "inverter_group_count": len(matches),
        "shared_parent_created": share_inverter,
    }


def clone_subarray(store, project: str, variant: str, source_subarray_id: int,
                   expected_sha256: str | None = None, clone_orientation: bool = False,
                   share_inverter: bool = False) -> dict:
    """Duplicate a grid subarray with all its exclusively owned inverter groups."""
    if not isinstance(source_subarray_id, int) or isinstance(source_subarray_id, bool):
        raise ValueError("source_subarray_id must be an integer")
    if not isinstance(clone_orientation, bool) or not isinstance(share_inverter, bool):
        raise ValueError("clone_orientation and share_inverter must be booleans")
    if share_inverter and clone_orientation:
        raise ValueError("Shared-parent cloning requires the existing common orientation")
    with store.lock:
        store._check_project(project)
        path = store._path(project, variant)
        data = store._read(path)
        store._check_hash(data, expected_sha256)
        reserved_ids = set()
        for item in store._root().iterdir():
            if (item.stem.casefold() == project[:-4].casefold()
                    and re.fullmatch(r"\.VC[A-Za-z0-9]+", item.suffix, re.I)):
                other_path = store._path(project, item.suffix[1:])
                _, other_lines = store._text(store._read(other_path))
                other_arrays, _ = store._sections(other_lines)
                reserved_ids.update(other_arrays)
        candidate, details = clone_subarray_data(
            store, data, source_subarray_id, clone_orientation=clone_orientation,
            share_inverter=share_inverter, reserved_ids=reserved_ids)
        backup = store._replace_checked(path, data, candidate, prepare=lambda: store._backup(path, data))
        return {"project": project, "variant": variant.upper(), **details,
                "backup_name": backup, "sha256": hashlib.sha256(candidate).hexdigest()}


def remove_subarray(store, project: str, variant: str, subarray_id: int,
                    expected_sha256: str | None = None, remove_orientation: bool = False,
                    shared_member: bool = False) -> dict:
    """Remove a subarray and its owned groups, retaining an orientation still in use."""
    if not isinstance(subarray_id, int) or isinstance(subarray_id, bool):
        raise ValueError("subarray_id must be an integer")
    if not isinstance(remove_orientation, bool) or not isinstance(shared_member, bool):
        raise ValueError("remove_orientation and shared_member must be booleans")
    if shared_member and remove_orientation:
        raise ValueError("Shared-member deletion preserves the common orientation")
    with store.lock:
        store._check_project(project)
        path = store._path(project, variant)
        data = store._read(path)
        store._check_hash(data, expected_sha256)
        _, lines = store._text(data)
        arrays, system = store._sections(lines)
        if subarray_id not in arrays or len(arrays) < 2 or system is None:
            raise ValueError("A target and another remaining subarray are required")
        kinds = store._field_indices(lines, system, "SystemType")
        if len(kinds) != 1 or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid":
            raise ValueError("Only grid subarrays are supported")
        store._require_no_geometry(lines, require_shading_section=True)
        bounds = arrays[subarray_id]
        no_orient = store._field_indices(lines, bounds, "NoOrientation")
        if len(no_orient) != 1:
            raise ValueError("Subarray needs one orientation")
        orientation = FIELD.fullmatch(lines[no_orient[0]].rstrip("\r\n"))[2]
        if not orientation.isdigit() or not 1 <= int(orientation) <= 2147483647:
            raise ValueError("Orientation ID must be a positive 32-bit integer")
        shared = any(other != subarray_id and any(
                (raw := FIELD.fullmatch(lines[i].rstrip("\r\n"))[2]).isdigit() and int(raw) == int(orientation)
                for i in store._field_indices(lines, other_bounds, "NoOrientation"))
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
            plane = store._orientation(lines, int(orientation))
            types = store._field_indices(lines, plane, "FieldType")
            if len(types) != 1 or FIELD.fullmatch(lines[types[0]].rstrip("\r\n"))[2] != "FixedPlane":
                raise ValueError("Shared-member deletion requires a fixed plane")

            def walk(node):
                yield node
                for child in node.children:
                    yield from walk(child)

            parent = next(node for tree in circuit_nodes(lines) for node in walk(tree) if node.line == groups[0]["line"])
            if parent.one("SubArrayId") != "0":
                raise ValueError("Shared-member deletion requires explicit parent owner 0")
            port = next((node for node in parent.children if node.one("SubArrayId") == str(subarray_id)), None)
            if port is None:
                raise ValueError("Shared-member deletion requires a direct port with the exact SubArrayId")
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
            matches, root = subarray_branches(lines, arrays, subarray_id)

        deletions = [bounds] + [(start, end) for start, end, _ in matches]
        orientation_id_map = {}
        if remove_orientation:
            counts = grid_inventory(lines, arrays)
            if counts["counts_valid"] is not True:
                raise ValueError("Orientation removal requires complete consistent grid counts")
            header, groups = store._orientation_list(lines)
            if len(groups) < 2:
                raise ValueError("Another orientation must remain")
            if [identifier for _, _, identifier in groups] != list(range(1, len(groups) + 1)):
                raise ValueError("Orientation removal requires sequential ordered IDs")
            plane = store._orientation(lines, int(orientation))
            fields = store._field_indices(lines, plane, "FieldType")
            if len(fields) != 1 or FIELD.fullmatch(lines[fields[0]].rstrip("\r\n"))[2] != "FixedPlane":
                raise ValueError("Only a fixed-plane orientation can be removed")
            references = [i for i, line in enumerate(lines) if (field := FIELD.fullmatch(line.rstrip("\r\n")))
                          and field[1] == "NoOrientation" and field[2].isdigit() and int(field[2]) == int(orientation)]
            if references != [bounds[0] + root.one("NoOrientation")[0]]:
                raise ValueError("Orientation has references outside its owning subarray")
            owned_references = set()
            for first, last, identifier in groups:
                store._ordinary_fixed_plane(lines, (first, last))
                index = store._field_indices(lines, (first, last), "NoOrient")[0]
                owned_references.add(index)
                if identifier > int(orientation):
                    orientation_id_map[str(identifier)] = identifier - 1
            for identifier, (first, last) in arrays.items():
                array_root = root if identifier == subarray_id else next(
                    block for block in objects("".join(lines[first:last + 1])) if not block.path)
                owned_branches, _ = subarray_branches(lines, arrays, identifier)
                require_plane_branch_mode(array_root, owned_branches)
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
        lines, _ = strip_result_cache(lines)
        candidate = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")
        _, checked = store._text(candidate)
        checked_arrays, _ = store._sections(checked)
        if subarray_id in checked_arrays or len(checked_arrays) != len(arrays) - 1:
            raise ValueError("Subarray removal failed validation")
        if shared_member:
            subarray_branches(checked, checked_arrays, remaining_member)
        if (len(matches) > 1 or remove_orientation or shared_member) and grid_inventory(checked, checked_arrays)["counts_valid"] is not True:
            raise ValueError("Remaining grouped grid counts failed validation")
        if remove_orientation:
            store._orientation_list(checked)
        backup = store._replace_checked(path, data, candidate, prepare=lambda: store._backup(path, data))
        return {
            "project": project,
            "variant": variant.upper(),
            "removed_subarray_id": subarray_id,
            "removed_orientation_id": int(orientation) if remove_orientation else None,
            "orientation_id_map": orientation_id_map,
            "remaining_member_id": remaining_member,
            "inverter_group_count": len(matches),
            "backup_name": backup,
            "sha256": hashlib.sha256(candidate).hexdigest(),
        }
