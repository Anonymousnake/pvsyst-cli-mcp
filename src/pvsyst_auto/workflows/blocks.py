"""Prepare a new fixed PV-only project by repeating complete native block templates."""
from __future__ import annotations

import re

from pvsyst_auto.components.editor import ASSIGNMENT, root_block
from pvsyst_auto.components.generator import direct_fields, one
from pvsyst_auto.project.circuit import circuit_nodes, inventory as grid_inventory


def _blocks_request(blocks):
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 100:
        raise ValueError("blocks must be a nonempty list of at most 100 block types")
    result, ids, sources = [], set(), set()
    for block in blocks:
        if not isinstance(block, dict) or set(block) != {"id", "source_subarray_id", "count"}:
            raise ValueError("Each block requires only id, source_subarray_id and count")
        identifier, source, count = (block[key] for key in ("id", "source_subarray_id", "count"))
        if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 100
                or any(ord(char) < 32 for char in identifier) or identifier in ids):
            raise ValueError("Block id must be unique, nonblank printable text of at most 100 characters")
        if (type(source) is not int or not 1 <= source <= 2147483647
                or type(count) is not int or not 1 <= count <= 100):
            raise ValueError("source_subarray_id and count must be positive strict integers; count is at most 100")
        if source in sources:
            raise ValueError("Each source_subarray_id must occur exactly once")
        ids.add(identifier)
        sources.add(source)
        result.append(dict(block))
    if sum(block["count"] for block in result) > 100:
        raise ValueError("The total block count must not exceed 100")
    return result


def _owner_values(root):
    return {key: [value for _, value in entries] for key, entries in root.fields.items()
            if key not in {"SubArrayId", "Comment"}}


def _inspect_blocks(store, data):
    _, lines = store._text(data)
    arrays, system = store._sections(lines)
    main = store._main_array(lines)
    if system is None or main is None:
        raise ValueError("Require a complete native main array and Grid system")
    first_child = next((index for index in range(1, len(lines))
                        if (match := ASSIGNMENT.fullmatch(lines[index].rstrip("\r\n")))
                        and match[2].startswith("PVObject_")), len(lines) - 1)
    if one(direct_fields(lines, (0, first_child)), "Version")[1] not in {"8.1.0", "8.1.6"}:
        raise ValueError("Block preparation requires native variant Version=8.1.0/8.1.6")
    main_fields = root_block(lines, main).fields
    for key, expected in (("Flags", 0x00100400), ("Flags2", 0x082A)):
        raw = one(main_fields, key)[1]
        if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", raw) or int(raw[1:], 16) != expected:
            raise ValueError("Block preparation requires the verified native main-array Flags=$00100400/Flags2=$082A profile")
    fields = direct_fields(lines, system)
    if one(fields, "SystemType")[1] != "Grid":
        raise ValueError("Only PV-only Grid block projects are supported")
    flags = one(fields, "Flags")[1]
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) or int(flags[1:], 16) != 0:
        raise ValueError("System grid limits, auxiliary powers, generator or other active system modes are unsupported")
    if set(fields) - {"Comment", "Flags", "SystemType"}:
        raise ValueError("Additional owning Grid system controls require separate review")
    for key in fields:
        one(fields, key)
    # Inspect actual object headers, never text in comments or historical chart caches.
    unsupported = {"pvLoad", "pvGridStorage", "pvBattery", "pvGenerator"}
    for line in lines:
        match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if match and match[2].startswith("PVObject_") and match[4] in unsupported:
            raise ValueError("Load, storage and generator objects are outside PV-only block preparation")
    if any((match := ASSIGNMENT.fullmatch(line.rstrip("\r\n")))
           and match[2].startswith("PVObject_") for line in lines[system[0] + 1:system[1]]):
        raise ValueError("Nested native Grid system objects require separate review")
    store._require_no_geometry(lines, require_shading_section=True)
    _, orientations = store._orientation_list(lines)
    planes = {}
    for first, last, identifier in orientations:
        store._ordinary_fixed_plane(lines, (first, last))
        planes[identifier] = "".join(lines[first:last + 1])

    def inspect_node(node):
        if node.kind == "TransformerNode":
            raise ValueError("Shared upstream native transformer configuration is not replicated by this operation")
        for child in node.children:
            inspect_node(child)

    for node in circuit_nodes(lines):
        inspect_node(node)
    inventory = grid_inventory(lines, arrays)
    if inventory["counts_valid"] is not True:
        raise ValueError("Require complete consistent grid counts: " + "; ".join(
            inventory["issues"] + inventory["unchecked_checks"]))
    rows = {row["id"]: row for row in inventory["subarrays"]}
    snapshots = {}
    for identifier in arrays:
        branches, root = store._subarray_branches(lines, arrays, identifier)
        store._require_plane_branch_mode(root, branches)
        if root.one("Version")[1] not in {"8.1.0", "8.1.6"}:
            raise ValueError("Block preparation requires native subarray Version=8.1.0/8.1.6")
        orientation = root.one("NoOrientation")[1]
        if not orientation.isdigit() or int(orientation) not in planes:
            raise ValueError("Each source block requires one declared ordinary fixed plane")
        if root.one("NStrOrient1")[1] != root.one("NStringCh")[1] or any(
                key.startswith("NStrOrient") and key != "NStrOrient1" and root.one(key)[1] != "0"
                for key in root.fields):
            raise ValueError("Each block must allocate all strings to its one existing orientation")
        snapshots[identifier] = {
            "orientation_id": int(orientation), "owner_values": _owner_values(root),
            "counts": rows[identifier], "inverter_group_count": len(branches),
            "component_refs": {"PAN": root.one("PVModule")[1], "OND": root.one("GInverter")[1]},
        }
    return inventory, snapshots, planes


def prepare_block_project(store, source_project, source_variant, new_project, blocks, dry_run=True):
    """Copy the selected PRJ/VC only and repeat every complete source block in memory.

    count includes the retained original block. All source subarrays must be covered
    exactly once. Weather, losses, component models and existing orientations remain
    inherited; no physical sizing, energy calculation, native call or model fitting.
    """
    result = {
        "operation": "block_project_preparation", "schema_version": 1,
        "status": "blocked", "dry_run": dry_run, "prepared": False, "simulated": False,
        "native_consumption_verified": False, "engineering_certified": False,
        "review_required": True, "source": {}, "working": {}, "plan": [], "mapping": [],
        "serialized_counts": None, "issues": [], "warnings": [
            "Only the verified main-array Flags=$00100400/Flags2=$082A profile with system Flags=0 and no circuit transformer is supported.",
            "Only explicitly complete native blocks are repeated; no report quantities, MPPT connections or angles are inferred.",
            "Source site/weather, losses, component models, orientations and metering boundary remain inherited and require engineering review.",
            "Preparation is not native loading, energy simulation, electrical validation or equipment certification.",
        ], "artifacts": {},
    }
    publishing = False
    try:
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be a strict boolean")
        plan = _blocks_request(blocks)
        result["plan"] = plan
        with store.lock:
            source_prj = store._path(source_project)
            source_vc = store._path(source_project, source_variant)
            target_prj = store._path(new_project)
            target_vc = store._path(new_project, source_vc.suffix[1:])
            result["source"] = {"project": source_prj.name, "variant": source_vc.suffix[1:]}
            result["working"] = {"project": target_prj.name, "variant": target_vc.suffix[1:]}
            if any(store._belongs_to_project(path, target_prj.name) for path in store._root().iterdir()):
                raise FileExistsError("Destination project or variant already exists")
            project_data = store._read(source_prj)
            store._project_text(project_data)
            original = store._read(source_vc)
            source_counts, source_blocks, source_planes = _inspect_blocks(store, original)
            if {block["source_subarray_id"] for block in plan} != set(source_blocks):
                raise ValueError("blocks must cover every source subarray exactly once")
            candidate = original
            mapping = []
            for block in plan:
                source_id = block["source_subarray_id"]
                identifiers = [source_id]
                for _ in range(block["count"] - 1):
                    candidate, details = store._clone_subarray_data(candidate, source_id)
                    identifiers.append(details["subarray_id"])
                saved = source_blocks[source_id]
                mapping.append({
                    "id": block["id"], "source_subarray_id": source_id,
                    "subarray_ids": identifiers, "orientation_id": saved["orientation_id"],
                    "source_counts": saved["counts"], "component_refs": saved["component_refs"],
                })
            final_counts, final_blocks, final_planes = _inspect_blocks(store, candidate)
            expected_ids = [identifier for row in mapping for identifier in row["subarray_ids"]]
            if (len(expected_ids) != len(set(expected_ids)) or set(final_blocks) != set(expected_ids)
                    or final_planes != source_planes):
                raise ValueError("Prepared block identities or source orientation records do not match the plan")
            for row in mapping:
                source = source_blocks[row["source_subarray_id"]]
                for identifier in row["subarray_ids"]:
                    actual = final_blocks[identifier]
                    if (actual["owner_values"] != source["owner_values"]
                            or actual["orientation_id"] != source["orientation_id"]
                            or actual["component_refs"] != source["component_refs"]
                            or actual["inverter_group_count"] != source["inverter_group_count"]
                            or actual["counts"]["circuit"] != source["counts"]["circuit"]
                            or actual["counts"]["declared_module_count"] != source["counts"]["declared_module_count"]):
                        raise ValueError(f"Prepared block {identifier} does not preserve its complete native source")
            result.update(mapping=mapping, serialized_counts=final_counts,
                          source_serialized_counts=source_counts,
                          totals={"blocks": len(final_blocks),
                                  "modules": sum(row["declared_module_count"] for row in final_counts["subarrays"]),
                                  **final_counts["totals"]})
            if store._read(source_prj) != project_data or store._read(source_vc) != original:
                raise ValueError("Source PRJ/VC changed while preparing the block project")
            if dry_run:
                result["status"] = "ready"
                return result
            publishing = True
            created = store._create_project_files(target_prj.name,
                [(target_prj, project_data), (target_vc, candidate)])
            result.update(status="prepared", prepared=True,
                          artifacts={"project": str(target_prj), "variant": str(target_vc)},
                          created_files=list(created["files"]))
    except (ValueError, OSError) as exc:
        # Nothing is written before publishing, so an unreadable or locked source stays blocked.
        result["status"] = "failed" if publishing else "blocked"
        result["issues"].append(str(exc))
    return result
