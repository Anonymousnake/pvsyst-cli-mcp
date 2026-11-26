"""Readonly reviews: native input comparison, structured brief arithmetic, and design input discovery."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, Overflow, localcontext
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr, model_validator

from pvsyst_auto.components.editor import objects, pan_validation_fields
from pvsyst_auto.components.store import electrical_inventory
from pvsyst_auto.engineering.sizing import plan_capacity
from pvsyst_auto.project.circuit import positive_integer
from pvsyst_auto.project.paths import project_name, safe_filename, variant_id, workspace_file

# --------------------------------------------------------------------- project input review
SAVED_TOLERANCE = Decimal("0.0005")


def _baseline_bytes(path):
    with path.open("rb") as stream:
        data = stream.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("Preparation baseline exceeds 32 MiB")
    return data


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("Expected a finite saved/declared number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Expected a finite saved/declared number") from exc
    if not result.is_finite():
        raise ValueError("Expected a finite saved/declared number")
    return result


def _within_saved_precision(actual, declared):
    """Match within the editor's three-decimal precision, not its re-serialized text."""
    with localcontext() as context:
        context.traps[Overflow] = False  # An absurd magnitude compares as different, never raises.
        return abs(_number(actual) - declared) <= SAVED_TOLERANCE


def review_project_inputs(store, *, project, variant, preparation_manifest_name):
    result = {"status": "blocked", "readonly": True, "issues": [], "comparisons": [], "declared_not_native": [],
              "native_consumption_verified": False, "runtime_state_observed": False, "repairs_applied": False}
    with store.lock:
        try:
            project, variant = project_name(project), variant_id(variant)
            path = workspace_file(store.workspace, "Results", preparation_manifest_name, ".json")
            baseline_bytes = _baseline_bytes(path)
            baseline = json.loads(baseline_bytes)
            if (not isinstance(baseline, dict) or baseline.get("operation") != "prepare_project"
                    or baseline.get("status") not in ("prepared", "needs_review") or baseline.get("prepared") is not True
                    or baseline.get("simulated") is not False or baseline.get("metrics") is not None
                    or not isinstance(baseline.get("request"), dict) or not isinstance(baseline.get("preparation_context"), dict)):
                raise ValueError("Require a completed source-preparation baseline, not an annual/job result")
            request, source, member = baseline["request"], project_name(baseline["working_project"]), variant_id(baseline["working_variant"])
            year = request["year"]
            if type(year) is not int or not 1969 <= year <= 2068:
                raise ValueError("Baseline year is outside the supported request range")
            expected_name = f"{Path(source).stem}_prepare_{year}.json"
            if path.name.casefold() != expected_name.casefold() or request.get("new_project") != source or request.get("variant") != member:
                raise ValueError("Baseline filename/project/variant declarations disagree")
            for key, extension in (("pan_name", ".PAN"), ("ond_name", ".OND"), ("site_name", ".SIT"), ("met_name", ".MET")):
                safe_filename(request[key], extension)
            _number(request["tilt"])
            if request.get("azimuth") is not None:
                _number(request["azimuth"])
            before, parameters, references = store.inspect_project(project), store.inspect_parameters(project, variant), store.inspect(project, variant)
            if (parameters["system_parameters"].get("SystemType") != "Grid"
                    or len(parameters["subarrays"]) != 1 or len(parameters["orientations"]) != 1 or len(references["subarrays"]) != 1):
                raise ValueError("Initial review supports one Grid subarray/orientation; no automatic topology mapping")
            array, orient = parameters["subarrays"][0], parameters["orientations"][0]
            values = orient["values"]
            if (values.get("FieldType") != "FixedPlane"
                    or any(values.get(key, "False") != "False" for key in ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver"))
                    or array["values"].get("NoOrientation") != str(orient["id"])):
                raise ValueError("Require a single assigned fixed non-seasonal plane for angle comparison")

            def compare(field, expected, actual, owner, unit=None, integer=False, text=False):
                if text:
                    same, saved = isinstance(actual, str) and actual.casefold() == expected.casefold(), expected
                else:
                    declared = _number(expected)
                    if integer:
                        if declared != declared.to_integral_value() or declared <= 0:
                            raise ValueError("Invalid baseline integer: " + field)
                        saved = str(int(declared))
                    else:
                        saved = format(float(declared), ".3f")
                    same = actual is not None and (_number(actual) == _number(saved) if integer else _within_saved_precision(actual, declared))
                result["comparisons"].append({"field": field, "owner": owner, "unit": unit, "declared": expected, "expected_saved": saved,
                                              "actual_saved": actual, "status": "missing" if actual is None else "matches" if same else "differs"})

            refs = references["subarrays"][0]["references"]
            for kind, key in (("PAN", "pan_name"), ("OND", "ond_name")):
                entries = refs.get(kind, [])
                if len(entries) > 1:
                    raise ValueError("Ambiguous owning component reference: " + kind)
                compare(key, request[key], entries[0] if entries else None, f"subarray:{array['id']}", text=True)
            actual_sources = before["sources"][store._path(project, variant).name]
            for key, kind in (("site_name", "site"), ("met_name", "meteo")):
                compare(key, request[key], actual_sources[kind]["name"], "variant_sources", text=True)
                compare("project_" + key, request[key], before["sources"][store._path(project).name][kind]["name"], "project_sources", text=True)
            compare("tilt", request["tilt"], values.get("FieldTilt"), f"orientation:{orient['id']}", "deg")
            if request.get("azimuth") is not None:
                compare("azimuth", request["azimuth"], values.get("FieldAzim"), f"orientation:{orient['id']}", "PVsyst_deg")
            plan = baseline.get("capacity_plan")
            if not isinstance(plan, dict) or plan.get("status") != "ready":
                raise ValueError("Baseline lacks its accepted capacity plan; counts are not inferred")
            for key, field in (("series_modules", "NModSerie"), ("string_count", "NStringCh"), ("inverter_count", "NInverter")):
                compare(field, plan[key], array["values"].get(field), f"subarray:{array['id']}", "count", integer=True)
            losses = request.get("losses") or {}
            if not isinstance(losses, dict) or set(losses) - {"LIDLoss", "PMismMPP", "ModuleQualityLoss", "UFactor"}:
                raise ValueError("Baseline contains an unsupported loss declaration")
            for key, value in losses.items():
                fields = ("PQualityModU", "PQualityModI") if key == "ModuleQualityLoss" else (key,)
                owner = "main_array" if key == "UFactor" else f"subarray:{array['id']}"
                actual = parameters["main_array"]["values"] if key == "UFactor" else array["values"]
                for field in fields:
                    compare(field, value, actual.get(field), owner, "W/m2/K" if key == "UFactor" else "%")
            project_roots = [b for b in objects(store._read(store._path(project)).decode("utf-8-sig")) if not b.path and b.object_type == "pvProject"]
            if len(project_roots) != 1:
                raise ValueError("Expected one owning project object")
            raw_project = {k: project_roots[0].one(k)[1] for k in
                           ("DesignTempMin", "TempOper", "TempOperMin", "TempOperMax", "TranspositionAlgorithm", "ACLossesRef") if k in project_roots[0].fields}
            context = baseline["preparation_context"]
            declared = context.get("request", {})
            if not isinstance(declared, dict):
                raise ValueError("Baseline preparation request must be an object")
            for key in ("electrical_design", "capacity"):
                if key in declared:
                    result["declared_not_native"].append({"field": key, "value": declared[key], "status": "not_directly_comparable",
                        "reason": "Declared envelope/goal is not a native project setting; no temperature/coefficient/current owner is guessed"})
            result["declared_not_native"].append({"field": "year", "value": year, "status": "execution_option_not_compared"})
            binding = context.get("source_binding") or {}
            if not isinstance(binding, dict):
                raise ValueError("Baseline source binding must be an object")
            if before["files"] != store.inspect_project(project)["files"] or _baseline_bytes(path) != baseline_bytes:
                raise ValueError("Baseline or target changed during review; no mixed-state result accepted")
            differences = [row for row in result["comparisons"] if row["status"] != "matches"]
            result.update(status="needs_review" if differences or losses or before["warnings"] or baseline["status"] == "needs_review"
                          or context.get("review_required") or binding.get("needs_review") else "reviewed",
                          source_reference_warnings=before["warnings"], project=project, variant=variant,
                          baseline_source={"project": source, "variant": member, "manifest": str(path)},
                          differences=differences, compared_fields=len(result["comparisons"]), inherited_project_saved=raw_project,
                          native_mode_flags={"subarray": array["values"].get("Flags"), "main_array": parameters["main_array"]["values"].get("Flags"),
                                             "main_array_flags2": parameters["main_array"]["values"].get("Flags2")},
                          loss_mode_interpretation_performed=False)
            return result
        except (ValueError, OSError, KeyError, TypeError) as exc:
            result.update(status="blocked", issues=[str(exc)], comparisons=[], declared_not_native=[])
            return result

# --------------------------------------------------------------------- engineering brief
BriefField = Literal[
    "module_count", "module_power_w", "module_vmp_v", "module_imp_a", "modules_per_string", "string_count",
    "inverter_count", "inverter_power_kw", "inverter_input_count", "block_count", "dc_capacity_kw", "ac_capacity_kw",
    "nominal_capacity_kw", "tilt_deg", "azimuth_deg", "clearance_m", "row_spacing_m", "storage_power_kw",
    "storage_energy_kwh", "pan_file", "ond_file", "hourly_weather_file", "module_manufacturer", "module_model",
    "inverter_manufacturer", "inverter_model", "storage_strategy", "weather_source", "mppt_string_allocation",
]
COUNT_FIELDS = {"module_count", "modules_per_string", "string_count", "inverter_count", "inverter_input_count", "block_count"}
TEXT_FIELDS = {"pan_file", "ond_file", "hourly_weather_file", "module_manufacturer", "module_model",
               "inverter_manufacturer", "inverter_model", "storage_strategy", "weather_source", "mppt_string_allocation"}
FIELD_UNITS = {
    **dict.fromkeys(COUNT_FIELDS, "count"), **dict.fromkeys(TEXT_FIELDS, "text"),
    "pan_file": "file_name", "ond_file": "file_name", "hourly_weather_file": "file_name",
    "module_power_w": "W", "module_vmp_v": "V", "module_imp_a": "A",
    **dict.fromkeys(("inverter_power_kw", "dc_capacity_kw", "ac_capacity_kw", "nominal_capacity_kw", "storage_power_kw"), "kW"),
    "storage_energy_kwh": "kWh", "tilt_deg": "deg", "azimuth_deg": "deg", "clearance_m": "m", "row_spacing_m": "m",
}
Identifier = Annotated[StrictStr, Field(min_length=1, max_length=100)]
PositiveCount = Annotated[StrictInt, Field(ge=1, le=1_000_000_000)]
Page = Annotated[StrictInt, Field(ge=1, le=1_000_000)]
Excerpt = Annotated[StrictStr, Field(min_length=1, max_length=2000)]


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class BriefSource(_StrictRequest):
    id: Identifier
    title: Annotated[StrictStr, Field(min_length=1, max_length=300)]
    locator: Annotated[StrictStr, Field(min_length=1, max_length=2000)]


class BriefClaim(_StrictRequest):
    id: Identifier
    field: BriefField
    scope: Identifier
    value: StrictInt | StrictFloat | StrictStr
    unit: StrictStr
    source_id: Identifier
    pdf_page: Page
    excerpt: Excerpt

    @model_validator(mode="after")
    def declared_type_and_unit(self):
        if self.unit != FIELD_UNITS[self.field]:
            raise ValueError(f"{self.field} requires unit {FIELD_UNITS[self.field]}")
        if self.field in TEXT_FIELDS:
            if not isinstance(self.value, str) or not self.value.strip() or len(self.value) > 500:
                raise ValueError(f"{self.field} requires nonblank text of at most 500 characters")
        else:
            if isinstance(self.value, (str, bool)):
                raise ValueError(f"{self.field} requires a strict number")
            if self.field in COUNT_FIELDS and not isinstance(self.value, int):
                raise ValueError(f"{self.field} requires a strict integer")
            low, high = (-180, 180) if self.field == "azimuth_deg" else (0, 90 if self.field == "tilt_deg" else 1e12)
            if self.value < low or self.value > high or (self.value == 0 and self.field not in {"azimuth_deg", "tilt_deg", "clearance_m"}):
                raise ValueError(f"{self.field} is outside its bounded declaration range")
        return self


class BriefBlock(_StrictRequest):
    id: Identifier
    count: PositiveCount
    strings_per_block: PositiveCount | None = None
    inverters_per_block: PositiveCount | None = None
    source_id: Identifier
    pdf_page: Page
    excerpt: Excerpt


class EngineeringBriefRequest(_StrictRequest):
    sources: Annotated[list[BriefSource], Field(min_length=1, max_length=50)]
    claims: Annotated[list[BriefClaim], Field(max_length=500)] = Field(default_factory=list)
    blocks: Annotated[list[BriefBlock], Field(max_length=100)] = Field(default_factory=list)


def review_engineering_brief(brief: EngineeringBriefRequest | dict) -> dict:
    """Aggregate bounded source conflicts, integer arithmetic and downstream missing inputs."""
    brief = EngineeringBriefRequest.model_validate(brief)
    for name, rows in (("sources", brief.sources), ("claims", brief.claims), ("blocks", brief.blocks)):
        ids = [row.id for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{name}: duplicate id")
        if any(not item.strip() for item in ids):
            raise ValueError(f"{name}: ids must not be blank")
    source_ids = {source.id for source in brief.sources}
    for row in [*brief.claims, *brief.blocks]:
        if row.source_id not in source_ids:
            raise ValueError(f"{row.id}: unknown source_id {row.source_id}")
    if any(not s.title.strip() or not s.locator.strip() for s in brief.sources):
        raise ValueError("Source title and locator must not be blank")
    if any(not c.scope.strip() or not c.excerpt.strip() for c in brief.claims):
        raise ValueError("Claim scope and excerpt must not be blank")
    if any(not b.excerpt.strip() for b in brief.blocks):
        raise ValueError("Block excerpt must not be blank")

    def refs(rows):
        return [{"claim_id" if isinstance(row, BriefClaim) else "block_id": row.id, "source_id": row.source_id,
                 "pdf_page": row.pdf_page, "excerpt": row.excerpt} for row in rows]

    def number(value):
        return int(value) if value == value.to_integral_value() else float(value)

    def decimal(claim):
        return Decimal(str(claim.value))

    groups = defaultdict(list)
    for claim in brief.claims:
        groups[(claim.scope, claim.field)].append(claim)
    selected, conflicts, checks, missing, review_items = {}, [], [], [], []
    for (scope, field), rows in groups.items():
        if len({(row.value, row.unit) for row in rows}) > 1:
            conflicts.append({"code": "conflicting_claims", "scope": scope, "field": field,
                              "message": "同一范围同一字段有多个声明值；请明确采信依据。",
                              "values": [row.value for row in rows], "refs": refs(rows)})
        else:
            selected[(scope, field)] = rows[0]

    def get(field, scope="project"):
        return selected.get((scope, field))

    def comparison(code, calculated, expected, formula, rows, scope="project", unit="count"):
        item = {"code": code, "scope": scope, "formula": formula, "unit": unit, "calculated": number(calculated), "refs": refs(rows)}
        if expected is not None:
            difference = calculated - decimal(expected)
            item.update(declared=expected.value, difference=number(difference), status="match" if difference == 0 else "mismatch")
            item["refs"] += refs(groups[(expected.scope, expected.field)])
            if difference != 0:
                conflicts.append({**item, "message": "明确输入的数量／名牌算术与该声明不一致。"})
        else:
            item["status"] = "calculated"
        checks.append(item)

    required = {
        "module_count": "确认全项目采用的组件总数。", "module_power_w": "提供精确组件名牌功率。",
        "modules_per_string": "确认整串串联块数。", "inverter_count": "确认逆变器总台数。",
        "inverter_power_kw": "提供单台逆变器名义AC功率。",
        "pan_file": "取得精确身份、完整曲线及合法来源的真实PAN；文件声明不证明其存在或认证。",
        "ond_file": "取得精确身份、完整曲线及合法来源的真实OND；文件声明不证明其存在或认证。",
        "hourly_weather_file": "提供具体年份、标准时间基准及来源的完整逐时天气资产。",
    }
    for field, action in required.items():
        if get(field) is None:
            missing.append({"field": field, "scope": "project", "reason": "conflicting" if ("project", field) in groups else "not_declared",
                            "action": action, "refs": refs(groups.get(("project", field), []))})

    modules, power, series = get("module_count"), get("module_power_w"), get("modules_per_string")
    inverters, ac_power = get("inverter_count"), get("inverter_power_kw")
    if modules and power:
        comparison("module_dc_capacity", decimal(modules) * decimal(power) / 1000, get("dc_capacity_kw"),
                   "module_count * module_power_w / 1000", [modules, power], unit="kW")
    if inverters and ac_power:
        comparison("inverter_ac_capacity", decimal(inverters) * decimal(ac_power), get("ac_capacity_kw"),
                   "inverter_count * inverter_power_kw", [inverters, ac_power], unit="kW")
    if modules and series:
        whole, remainder = divmod(modules.value, series.value)
        item = {"code": "whole_strings", "scope": "project", "formula": "divmod(module_count, modules_per_string)",
                "full_strings": whole, "remainder_modules": remainder, "refs": refs([modules, series]),
                "status": "match" if remainder == 0 else "mismatch"}
        checks.append(item)
        if remainder:
            conflicts.append({**item, "message": "组件总数不能组成整数整串；不自动增减组件。"})
        else:
            comparison("string_count", Decimal(whole), get("string_count"), "module_count / modules_per_string", [modules, series])
    if modules and power and inverters and ac_power:
        checks.append({"code": "nominal_dc_ac_ratio", "scope": "project",
                       "formula": "module_count * module_power_w / 1000 / (inverter_count * inverter_power_kw)",
                       "value": number(decimal(modules) * decimal(power) / 1000 / (decimal(inverters) * decimal(ac_power))),
                       "refs": refs([modules, power, inverters, ac_power]), "meaning": "名牌算术比，不是允许上限或电气校核。"})

    for scope in sorted({claim.scope for claim in brief.claims}):
        pmax, vmp, imp = (get(field, scope) for field in ("module_power_w", "module_vmp_v", "module_imp_a"))
        if pmax and vmp and imp:
            product = decimal(vmp) * decimal(imp)
            difference = decimal(pmax) - product
            item = {"code": "module_nameplate_product", "scope": scope, "formula": "module_power_w - module_vmp_v * module_imp_a",
                    "unit": "W", "declared_pmax": pmax.value, "vmp_times_imp": number(product),
                    "difference": number(difference), "refs": refs([pmax, vmp, imp])}
            checks.append(item)
            if difference != 0:
                review_items.append({**item, "message": "核对名牌身份、参考条件和显示精度；差值不作物理模型判定。"})
        inputs = get("inverter_input_count", scope)
        if inputs and not get("mppt_string_allocation", scope):
            review_items.append({"code": "input_allocation_unresolved", "scope": scope,
                                 "message": "输入路数未给出每路／MPPT并串和分配，不能据此判定绝对可接性。",
                                 "refs": refs([inputs]), "action": "提供实际MPPT、输入路和串组分配及厂家依据。"})

    if brief.blocks:
        block_count = sum(block.count for block in brief.blocks)
        comparison("block_count", Decimal(block_count), get("block_count"), "sum(block.count)", brief.blocks)
        for attribute, expected, code in (("strings_per_block", get("string_count"), "block_string_count"),
                                          ("inverters_per_block", inverters, "block_inverter_count")):
            absent = [block for block in brief.blocks if getattr(block, attribute) is None]
            if absent:
                missing.append({"field": f"blocks.{attribute}", "scope": "project", "reason": "incomplete_block_schedule",
                                "action": "补齐每种分块的实际整数数量，不从输入路数推断。", "refs": refs(absent)})
                continue
            total = sum(block.count * getattr(block, attribute) for block in brief.blocks)
            comparison(code, Decimal(total), expected, f"sum(block.count * block.{attribute})", brief.blocks)
            if attribute == "strings_per_block" and series:
                comparison("block_module_count", Decimal(total) * decimal(series), modules,
                           "sum(block.count * block.strings_per_block) * modules_per_string", [*brief.blocks, series])
        if block_count > 1:
            review_items.append({"code": "multiple_blocks", "scope": "project",
                                 "message": "分块算术不构成原生拓扑；当前统一新建路径限均匀单子阵，需实际分块接线／原生工程。", "refs": refs(brief.blocks)})

    storage_power, storage_energy = get("storage_power_kw"), get("storage_energy_kwh")
    if any(("project", f) in groups for f in ("storage_power_kw", "storage_energy_kwh", "storage_strategy")):
        for field, action in (("storage_power_kw", "明确储能功率额定对象、AC/DC侧及原生设备依据。"),
                              ("storage_energy_kwh", "明确名义／可用容量及原生电池硬件依据。"),
                              ("storage_strategy", "明确原生支持策略；不自动生成充放电或电价调度。")):
            if get(field) is None:
                missing.append({"field": field, "scope": "project", "reason": "conflicting" if ("project", field) in groups else "not_declared",
                                "action": action, "refs": refs(groups.get(("project", field), []))})
    if storage_power and storage_energy:
        checks.append({"code": "storage_nominal_hours", "scope": "project", "formula": "storage_energy_kwh / storage_power_kw",
                       "value": number(decimal(storage_energy) / decimal(storage_power)), "unit": "h",
                       "refs": refs([storage_energy, storage_power])})
        review_items.append({"code": "storage_basis", "scope": "project",
                             "message": "需真实BTR／PCS、SOC、效率、温度、SOW、连接与策略；不由MW/MWh推断。",
                             "refs": refs([storage_power, storage_energy]), "action": "先确认用途和原生支持策略，再准备对应负荷／可用性及硬件依据。"})

    next_actions = [item["action"] for item in missing]
    if conflicts:
        next_actions.insert(0, "逐项解决引用冲突和整数／容量差异，明确采信值；不自动选章节或凑整。")
    next_actions += [item["action"] for item in review_items if "action" in item]
    next_actions.append("核对原文及工程依据；精确资产声明和数量一致后仍需受支持的原生工程准备／运行验证。")
    return {"operation": "engineering_brief_review", "schema_version": 1, "status": "blocked" if conflicts or missing else "needs_review",
            "readonly": True, "simulated": False, "native_consumption_verified": False,
            "sources": [s.model_dump() for s in brief.sources], "facts": [c.model_dump() for c in brief.claims],
            "blocks": [b.model_dump() for b in brief.blocks], "checks": checks, "conflicts": conflicts,
            "missing": missing, "review_items": review_items, "next_actions": list(dict.fromkeys(next_actions))}

# --------------------------------------------------------------------- design input discovery
SELECTION_RULE = (
    "Components: exact case-insensitive Manufacturer+Model equality over workspace and installed loose files "
    "(or one explicit filename); workspace loose files win over the installed library. One usable match is "
    "selected, two or more usable matches in the winning library are reported as ambiguous and block the "
    "discovery; no fuzzy, approximate or proxy component substitution and no encrypted-database enumeration. "
    "Templates: only workspace PRJ/VC members that pass one subarray, one ordinary fixed plane, Grid, "
    "PRCorrTemp=False and no-active-geometry checks are eligible. With capacity every eligible member must "
    "return a ready plan_capacity result, and members are ordered by fewest inverter changes from the "
    "template counts, then fewest parameter steps, then capacity error, then the stable project/variant name."
)


def _public(candidate: dict) -> dict:
    return {key: value for key, value in candidate.items() if not key.startswith("_")}


def _loose_files(components, kind: str, library: str, query: str) -> list[dict]:
    items, offset = [], 0
    while True:
        page = components.list(kind, library, query=query, offset=offset, limit=200)
        items.extend(page["items"])
        offset += len(page["items"])
        if not page["items"] or offset >= page["total"]:
            return items


def _resolve(components, kind: str, name: str, manufacturer: str, model: str, result: dict) -> list[dict]:
    candidates = []
    for library in ("workspace", "builtin"):
        if name:
            try:
                path = components._path(kind, name, library)
            except ValueError as exc:
                if library == "builtin" and components.builtin is None:
                    result["warnings"].append(f"{kind}: no installed component library is configured; only workspace loose files were searched")
                    continue
                result["issues"].append({"field": f"{kind}.name", "message": str(exc)})
                return []
            if path.is_file():
                candidates.append({"library": library, "name": name})
            continue
        try:
            found = _loose_files(components, kind, library, model)
        except (ValueError, OSError) as exc:
            if library == "builtin" and components.builtin is None:
                result["warnings"].append(f"{kind}: no installed component library is configured; only workspace loose files were searched")
            else:
                result["warnings"].append(f"{kind}: {library} loose files are unavailable ({exc}); the encrypted built-in database is not enumerable")
            continue
        for item in found:
            if (item.get("manufacturer") or "").casefold() == manufacturer.casefold() and (item.get("model") or "").casefold() == model.casefold():
                candidates.append({"library": library, "name": item["name"]})
    candidates.sort(key=lambda item: (item["library"] != "workspace", item["name"].casefold()))
    if not candidates:
        target = f"file {name}" if name else f"{manufacturer} {model}"
        result["issues"].append({"field": f"{kind}.name", "message": f"No loose file matches {target} in the workspace or the installed library; "
                                 "the encrypted built-in database cannot be enumerated and no approximate identity is substituted"})
        return []
    for candidate in candidates:
        _verify(components, kind, candidate, manufacturer, model, result)
    return candidates


def _verify(components, kind: str, candidate: dict, manufacturer: str, model: str, result: dict) -> None:
    errors = []
    try:
        path = components._path(kind, candidate["name"], candidate["library"])
        raw = components._read(path)
        components._require_valid(info := components._inspect(kind, raw), complete=True)
        candidate.update(format=info["format"], manufacturer=info.get("manufacturer"), model=info.get("model"),
                         path=str(path), _text=raw.decode("utf-8-sig"))
    except (ValueError, OSError) as exc:
        candidate.update(format=None, manufacturer=None, model=None, path=None, valid=False, errors=[str(exc)])
        return
    if manufacturer and (candidate["manufacturer"] or "").casefold() != manufacturer.casefold():
        errors.append(f"Declared manufacturer {manufacturer!r} does not match {candidate['manufacturer']!r}")
    if model and (candidate["model"] or "").casefold() != model.casefold():
        errors.append(f"Declared model {model!r} does not match {candidate['model']!r}")
    candidate.update(valid=not errors, errors=errors)
    if candidate["library"] == "builtin":
        try:
            shadow = components._path(kind, candidate["name"]).is_file()
        except ValueError:
            shadow = False
        if shadow:
            result["warnings"].append(f"{kind}: workspace file {candidate['name']} shadows the installed-library match; the installed identity is not substituted")
            candidate.update(valid=False, errors=[*candidate["errors"], "Shadowed by a workspace file with the same name; identity is not substituted"])


def _bound(kind: str, candidate: dict, result: dict) -> dict:
    """Attach declared reference-point fields for the selected component only."""
    try:
        candidate["electrical"] = electrical_inventory(kind, candidate["_text"])
    except (ValueError, KeyError) as exc:
        candidate["electrical"] = {"status": "unreadable", "issues": [str(exc)]}
        result["warnings"].append(f"{kind}: declared electrical fields are unreadable ({exc})")
    return candidate


def _select(kind: str, candidates: list[dict], result: dict) -> dict | None:
    usable = [item for item in candidates if item.get("valid")]
    workspace, installed = [i for i in usable if i["library"] == "workspace"], [i for i in usable if i["library"] == "builtin"]
    if len(workspace) > 1:
        result["issues"].append({"field": f"{kind}.name", "message": f"{len(workspace)} usable workspace files share this identity; pass one filename explicitly: "
                                 + ", ".join(item["name"] for item in workspace)})
        return None
    if workspace:
        if installed:
            result["warnings"].append(f"{kind}: workspace {workspace[0]['name']} is used and takes precedence over the installed-library match")
        return _bound(kind, workspace[0], result)
    if len(installed) > 1:
        result["issues"].append({"field": f"{kind}.name", "message": f"{len(installed)} usable installed-library files share this identity; pass one filename explicitly: "
                                 + ", ".join(item["name"] for item in installed)})
        return None
    if installed:
        return _bound(kind, installed[0], result)
    if candidates:
        result["issues"].append({"field": f"{kind}.name", "message": "No usable component: " + "; ".join(
            f"{i['name']} ({i['library']}): " + "; ".join(i.get("errors") or ["unusable"]) for i in candidates)})
    return None


def _report(project: str, variant: str, reasons: list[str]) -> dict:
    return {"project": project, "variant": variant, "valid": False, "reasons": reasons, "subarray_id": None,
            "orientation_id": None, "counts": {}, "capacity_plan": None, "_key": (project.casefold(), variant)}


def _pairs(store, template_project: str, variant: str, result: dict) -> tuple[list[tuple[str, str]], list[dict]]:
    try:
        root = store._root()
    except ValueError as exc:
        result["issues"].append({"field": "template_project", "message": str(exc)})
        return [], []
    if template_project:
        names = [template_project]
    else:
        try:
            names = sorted((p.name for p in root.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.upper() == ".PRJ"), key=str.casefold)
        except OSError as exc:
            result["issues"].append({"field": "template_project", "message": f"Workspace projects are unreadable: {exc}"})
            return [], []
        if not names:
            result["issues"].append({"field": "template_project", "message": "No readable workspace PRJ with a variant was found"})
            return [], []
    wanted, pairs, rejected = variant_id(variant) if variant else None, [], []
    for name in names:
        try:
            info = store.inspect_project(name)
        except (ValueError, OSError) as exc:
            if template_project:
                result["issues"].append({"field": "template_project", "message": f"{name}: {exc}"})
            else:
                rejected.append(_report(name, wanted or "", [f"Project is not readable: {exc}"]))
            continue
        members = sorted(info["variants"], key=str.casefold)
        if not members:
            if template_project:
                result["issues"].append({"field": "variant", "message": f"{name}: no workspace variant"})
            else:
                rejected.append(_report(name, wanted or "", ["Project has no workspace variant member"]))
            continue
        if wanted is not None:
            if wanted not in [m.upper() for m in members]:
                if template_project:
                    result["issues"].append({"field": "variant", "message": f"{name}: variant {wanted} is missing"})
                else:
                    rejected.append(_report(name, wanted, [f"Variant {wanted} is missing from this project"]))
                continue
            members = [wanted]
        pairs.extend((name, m.upper()) for m in members)
    return pairs, rejected


def _preflight(store, project: str, variant: str, capacity, pan_text, ond_text) -> dict:
    report = _report(project, variant, [])
    try:
        parameters, structure = store.inspect_parameters(project, variant), store.validate_structure(project, variant)
        if structure["valid"] is not True or structure["system_type"] != "Grid":
            raise ValueError("Require a completely checked Grid template: " + "; ".join(structure["issues"] + structure["unchecked_checks"]))
        if len(parameters["subarrays"]) != 1 or len(parameters["orientations"]) != 1:
            raise ValueError("Require exactly one subarray and one fixed plane")
        _, lines = store._text(store._read(store._path(project, variant)))
        child = next(i for i in range(1, len(lines)) if lines[i].strip().startswith("PVObject_"))
        corrected = store._field_indices(lines, (0, child), "PRCorrTemp")
        if len(corrected) != 1 or lines[corrected[0]].split("=", 1)[1].strip() != "False":
            raise ValueError("Template must explicitly declare PRCorrTemp=False")
        store._require_no_geometry(lines)
        array, plane = parameters["subarrays"][0], parameters["orientations"][0]["id"]
        store._ordinary_fixed_plane(lines, store._orientation(lines, plane), allow_missing_flags=True)
        counts = {key: array["values"].get(key) for key in ("NModSerie", "NStringCh", "NInvMPPT", "NInverter")}
        report.update(subarray_id=array["id"], orientation_id=plane, counts=counts)
    except (ValueError, OSError, KeyError, StopIteration) as exc:
        report["reasons"].append(str(exc))
        return report
    if capacity is None:
        report["valid"] = True
        return report
    if pan_text is None or ond_text is None:
        report["reasons"].append("Capacity planning requires the resolved PAN and OND components")
        return report
    try:
        plan = plan_capacity(store, lines, array["id"], pan_validation_fields(pan_text), electrical_inventory("OND", ond_text), capacity)
    except (ValueError, OSError, KeyError) as exc:
        report["reasons"].append(str(exc))
        return report
    report["capacity_plan"] = plan
    if plan["status"] != "ready":
        report["reasons"].extend(f"{item['field']}: {item['message']}" for item in plan["issues"])
        return report
    try:
        current = positive_integer(counts.get("NInverter"), "Template NInverter")
    except ValueError as exc:
        report["reasons"].append(str(exc))
        return report
    report["valid"] = True
    report["_key"] = (abs(plan["inverter_count"] - current), len(plan["steps"]),
                      round(abs(plan["capacity_error_pct"]), 9), project.casefold(), variant)
    return report


def discover_design_inputs(store, *, pan_name="", ond_name="", module_manufacturer="", module_model="",
                           inverter_manufacturer="", inverter_model="", template_project="",
                           variant="", capacity=None, tilt=None):
    """Locate one exact PAN/OND pair and one eligible template variant without writing or guessing."""
    components = getattr(store, "components", store)
    result = {"status": "blocked", "selected": None, "resolved_components": None,
              "candidates": {"PAN": [], "OND": [], "templates": []},
              "issues": [], "warnings": [], "selection_rule": SELECTION_RULE}

    def issue(field, message):
        result["issues"].append({"field": field, "message": str(message)})

    if not hasattr(store, "_root"):
        issue("store", "Discovery requires a VariantStore for workspace projects and component paths")

    chosen = {}
    for kind, name, manufacturer, model in (("PAN", pan_name, module_manufacturer, module_model),
                                            ("OND", ond_name, inverter_manufacturer, inverter_model)):
        if not name and not (manufacturer and model):
            issue(f"{kind}.name", "Provide one exact Manufacturer+Model pair or an explicit loose filename")
            continue
        try:
            candidates = _resolve(components, kind, name, manufacturer, model, result)
        except (ValueError, OSError) as exc:
            issue(f"{kind}.name", exc)
            continue
        result["candidates"][kind] = [_public(item) for item in candidates]
        if selected := _select(kind, candidates, result):
            chosen[kind] = selected

    reports, eligible = [], []
    if not result["issues"]:
        try:
            pairs, reports = _pairs(store, template_project, variant, result)
        except ValueError as exc:
            issue("variant", exc)
            pairs, reports = [], []
        for project, member in pairs:
            reports.append(_preflight(store, project, member, capacity,
                                      (chosen.get("PAN") or {}).get("_text"), (chosen.get("OND") or {}).get("_text")))
        result["candidates"]["templates"] = [_public(report) for report in reports]
        eligible = [report for report in reports if report["valid"]]
        for report in reports:
            if not report["valid"]:
                reason = "; ".join(report["reasons"])
                if eligible:
                    result["warnings"].append(f"{report['project']}/{report['variant']} is not eligible: {reason}")
                else:
                    issue(f"template {report['project']}/{report['variant']}", reason)

    template = min(eligible, key=lambda report: report["_key"]) if eligible else None
    if len(chosen) == 2:
        result["resolved_components"] = {"PAN": _public(chosen["PAN"]), "OND": _public(chosen["OND"])}
    if template is not None and len(chosen) == 2 and not result["issues"]:
        result["selected"] = {"PAN": _public(chosen["PAN"]), "OND": _public(chosen["OND"]), "template": _public(template)}
        result["status"] = "ready"
    return result
