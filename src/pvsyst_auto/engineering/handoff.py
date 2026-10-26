"""Readonly engineering handoff from saved PVsyst project and variant records."""
from __future__ import annotations

import json
import ntpath

from pvsyst_auto.components.editor import objects
from pvsyst_auto.project.paths import project_name, safe_filename, variant_id, workspace_file
from pvsyst_auto.project.tracking import _view


def _declarations(value):
    if value is None:
        return {}
    if (not isinstance(value, dict) or len(value) > 64
            or any(not isinstance(key, str) or not key.strip() or len(key) > 128 for key in value)):
        raise ValueError("Declarations must be an object with at most 64 named engineering inputs")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("Engineering declarations exceed 64 KiB")
    return json.loads(encoded)


def _root_values(block, names):
    return {name: block.one(name)[1] for name in names if name in block.fields}


def _load_records(blocks):
    records = []
    for block in blocks:
        if block.object_type.casefold() != "pvload":
            continue
        values, omitted = {}, []
        for name in sorted(block.fields):
            value = block.one(name)[1]
            if (len(value) > 512 or name.casefold().startswith(("day_", "valday"))
                    or (name.casefold().startswith("valhour") and block.object_type.casefold() != "pvload")
                    or name.casefold() in ("hourlyvalues", "hourlydata")):
                omitted.append(name)
            else:
                values[name] = value
        records.append({"type": block.object_type, "path": block.path,
                        "values": values, "omitted_cache_fields": omitted})
    return {"present": bool(records), "objects": records, "hourly_cache_contents_included": False}


def build_project_handoff(store, *, project, variant, declarations=None):
    """Describe saved inputs; neither declarations nor readable files certify a design."""
    result = {"status": "blocked", "readonly": True, "issues": [], "saved": {},
              "declarations": {}, "external_dependencies": [], "missing_items": [],
              "review_items": [], "native_consumption_verified": False,
              "runtime_state_observed": False,
              "scope": "Saved inputs and explicit caller declarations; no physical, source or native-consumption certification"}
    with store.lock:
        try:
            project, variant = project_name(project), variant_id(variant)
            declared = _declarations(declarations)
            project_path, variant_path = store._path(project), store._path(project, variant)
            project_bytes, variant_bytes = store._read(project_path), store._read(variant_path)
            store._project_text(project_bytes)
            project_text = project_bytes.decode("utf-8-sig")
            _, lines = store._text(variant_bytes)
            project_blocks = [block for block in objects(project_text)
                              if not block.path and block.object_type == "pvProject"]
            if len(project_blocks) != 1:
                raise ValueError("Expected one owning native project record")
            root = project_blocks[0]
            parameters = store.inspect_parameters(project, variant)
            references = store.inspect(project, variant)
            structure = store.validate_structure(project, variant)
            blocks = list(objects(_view(lines)))
            dependencies, missing, review = [], [], []

            def dependency(kind, name, resolution, action=None):
                item = {"kind": kind, "name": name, "resolution": resolution}
                if action:
                    item["required_action"] = action
                if item not in dependencies:
                    dependencies.append(item)
                return item

            def source(owner, kind, name):
                extension, folder = (".SIT", "Sites") if kind == "site" else (".MET", "Meteo")
                if not name:
                    resolution = "missing"
                else:
                    try:
                        file = workspace_file(store.workspace, folder, name, extension)
                        resolution = "workspace" if file.is_file() else "missing"
                    except (ValueError, OSError):
                        resolution = "invalid"
                action = None if resolution == "workspace" else "提供工程实际引用的工作区文件，并确认来源、年份与时间基准。"
                dependency(kind, name, resolution, action)
                if action:
                    missing.append({"field": owner + "." + kind, "reason": resolution, "action": action})
                return {"name": name, "workspace_file": resolution == "workspace"}

            source_names = {
                "project": {"site": store._embedded_reference(project_text.splitlines(), "PVObject_SitePrj=pvSite", "End of PVObject pvSite"),
                            "meteo": root.one("MeteoFileName")[1] if "MeteoFileName" in root.fields else None},
                "variant": {"site": store._embedded_reference(lines, "PVObject_SiteSimul=pvSite", "End of PVObject pvSite"),
                            "meteo": store._embedded_reference(lines, "PVObject_MeteoSimul=pvMeteo", "End of PVObject pvMeteo")}}
            sources = {owner: {kind: source(owner, kind, name) for kind, name in names.items()}
                       for owner, names in source_names.items()}
            for kind in ("site", "meteo"):
                names = [source_names[owner][kind] for owner in ("project", "variant")]
                if all(names) and names[0].casefold() != names[1].casefold():
                    review.append("PRJ与所选VC保存的" + kind + "引用不同；确认本次运行实际采用的输入。")

            component_refs = [(kind, name) for row in references["subarrays"]
                              for kind, names in row["references"].items() for name in names]
            component_refs.extend((kind, name) for kind, names in references["system_references"].items() for name in names)
            component_refs.extend((kind, block.one(field)[1]) for block in blocks
                                  for kind, field in (("BTR", "BatteryFile"), ("GEN", "GensetFile"))
                                  if field in block.fields)
            for kind, name in component_refs:
                resolution = "native_database_or_unavailable"
                try:
                    for library in ("workspace", "builtin"):
                        if library == "builtin" and store.components.builtin is None:
                            continue
                        if store.components._path(kind, name, library).is_file():
                            resolution = "workspace" if library == "workspace" else "builtin_file"
                            break
                except (ValueError, OSError):
                    resolution = "invalid"
                action = None if resolution in ("workspace", "builtin_file") else "在PVsyst中确认该原生数据库模型可用，或提供有权使用的完整导出模型；不按文件名猜替代设备。"
                dependency(kind, name, resolution, action)
                if action:
                    review.append(kind + ": " + name + "；" + action)

            ref_arrays = {row["id"]: row["references"] for row in references["subarrays"]}
            subarrays = [{**row, "references": ref_arrays.get(row["id"], {})} for row in parameters["subarrays"]]
            for row in subarrays:
                counts = [row["values"].get(field, "") for field in ("NModSerie", "NStringCh")]
                row["declared_module_count"] = int(counts[0]) * int(counts[1]) if all(value.isdigit() for value in counts) else None
                for kind in ("PAN", "OND"):
                    if len(row["references"].get(kind, [])) != 1:
                        missing.append({"field": f"subarray:{row['id']}.{kind}", "reason": "missing_or_ambiguous_reference",
                                        "action": "确认该子阵保存的唯一设备引用。"})
            load = _load_records(blocks)
            for row in load["objects"]:
                values = row["values"]
                if values.get("HourlyLoad") != "THourlyParams":
                    continue
                name, saved_path = values.get("FileName"), values.get("PathName")
                matches = False
                try:
                    safe_filename(name, ".csv")
                    file = workspace_file(store.workspace, "UserHourlyParams", name, ".csv")
                    resolution = "workspace" if file.is_file() else "missing"
                    matches = isinstance(saved_path, str) and bool(saved_path) and (
                        ntpath.normcase(ntpath.normpath(saved_path)) == ntpath.normcase(ntpath.normpath(str(file.parent))))
                except (ValueError, OSError):
                    resolution = "invalid"
                row["hourly_source"] = {"file_name": name, "saved_path": saved_path,
                    "workspace_folder": "UserHourlyParams", "workspace_file": resolution == "workspace",
                    "runtime_binding_verified": False}
                action = "确认负荷来源、年份、单位与时基，并由运行入口核对原生小时快照；保存路径或同名CSV不证明已经消费。"
                item = dependency("load", name, resolution, action)
                item.update(saved_path=saved_path, workspace_folder="UserHourlyParams", path_matches_workspace=matches)
                if resolution != "workspace":
                    missing.append({"field": "load.FileName", "reason": resolution,
                                    "action": "在UserHourlyParams提供该负荷源文件及年份、单位、时间基准说明；不读取保存路径指向的外部文件。"})
                if not matches:
                    review.append("小时负荷保存的PathName与当前工作区UserHourlyParams不一致；这里只列同名工作区文件，不跟随外部路径。")
            review.extend(str(item) for item in structure.get("issues", []))
            review.extend(str(item) for item in structure.get("warnings", []))
            review.extend("未完成的保存结构检查：" + str(item) for item in structure.get("unchecked_checks", []))
            if not declared:
                missing.append({"field": "declarations", "reason": "engineering_basis_not_supplied",
                                "action": "补充本项目设备来源、天气来源及现场设计条件；保存值和软件默认值不自动成为工程依据。"})
            if store._read(project_path) != project_bytes or store._read(variant_path) != variant_bytes:
                raise ValueError("Project or variant changed while building the engineering handoff")
            saved = {"project": {"name": root.one("Comment")[1] if "Comment" in root.fields else None,
                                 "variant_name": parameters.get("variant_name"),
                                 "values": _root_values(root, ("DesignTempMin", "TempOper", "TempOperMin", "TempOperMax", "TranspositionAlgorithm", "ACLossesRef"))},
                     "system": parameters["system_parameters"], "orientations": parameters["orientations"],
                     "subarrays": subarrays, "grid_circuit": structure.get("grid_circuit"),
                     "sources": sources, "load": load, "storage": parameters.get("grid_storage"),
                     "main_array": parameters.get("main_array"), "shading_scene": parameters.get("shading_scene")}
            result.update(status="needs_review" if missing or review else "ready", project=project, variant=variant,
                          saved=saved, declarations=declared, external_dependencies=dependencies,
                          missing_items=missing, review_items=list(dict.fromkeys(review)),
                          input_snapshot={"project_file": project_path.name, "variant_file": variant_path.name,
                                          "variant_sha256": parameters["sha256"]},
                          inherited_values="All saved values are inherited disk state; declarations are listed separately and do not overwrite them")
            return result
        except (ValueError, OSError, KeyError, TypeError) as exc:
            result.update(status="blocked", issues=[str(exc)], saved={})
            return result
