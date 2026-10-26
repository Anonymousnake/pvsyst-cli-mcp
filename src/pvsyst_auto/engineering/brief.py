"""Read-only arithmetic and source handoff for a caller-structured engineering brief.

This is not a PDF reader, electrical designer, model fitter or PVsyst adapter.
All sources and excerpts are caller declarations; no locator is opened.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (BaseModel, ConfigDict, Field, StrictFloat, StrictInt,
                      StrictStr, model_validator)


BriefField = Literal[
    "module_count", "module_power_w", "module_vmp_v", "module_imp_a",
    "modules_per_string", "string_count", "inverter_count", "inverter_power_kw",
    "inverter_input_count", "block_count", "dc_capacity_kw", "ac_capacity_kw",
    "nominal_capacity_kw", "tilt_deg", "azimuth_deg", "clearance_m",
    "row_spacing_m", "storage_power_kw", "storage_energy_kwh",
    "pan_file", "ond_file", "hourly_weather_file", "module_manufacturer",
    "module_model", "inverter_manufacturer", "inverter_model",
    "storage_strategy", "weather_source", "mppt_string_allocation",
]
COUNT_FIELDS = {
    "module_count", "modules_per_string", "string_count", "inverter_count",
    "inverter_input_count", "block_count",
}
TEXT_FIELDS = {
    "pan_file", "ond_file", "hourly_weather_file", "module_manufacturer",
    "module_model", "inverter_manufacturer", "inverter_model",
    "storage_strategy", "weather_source", "mppt_string_allocation",
}
FIELD_UNITS = {
    **dict.fromkeys(COUNT_FIELDS, "count"),
    **dict.fromkeys(TEXT_FIELDS, "text"),
    "pan_file": "file_name", "ond_file": "file_name", "hourly_weather_file": "file_name",
    "module_power_w": "W", "module_vmp_v": "V", "module_imp_a": "A",
    **dict.fromkeys(("inverter_power_kw", "dc_capacity_kw", "ac_capacity_kw",
                     "nominal_capacity_kw", "storage_power_kw"), "kW"),
    "storage_energy_kwh": "kWh", "tilt_deg": "deg", "azimuth_deg": "deg",
    "clearance_m": "m", "row_spacing_m": "m",
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
            low, high = (-180, 180) if self.field == "azimuth_deg" else (0, 1e12)
            if self.field == "tilt_deg":
                high = 90
            inclusive_zero = self.field in {"azimuth_deg", "tilt_deg", "clearance_m"}
            if self.value < low or self.value > high or (self.value == 0 and not inclusive_zero):
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
    if any(not source.title.strip() or not source.locator.strip() for source in brief.sources):
        raise ValueError("Source title and locator must not be blank")
    if any(not claim.scope.strip() or not claim.excerpt.strip() for claim in brief.claims):
        raise ValueError("Claim scope and excerpt must not be blank")
    if any(not block.excerpt.strip() for block in brief.blocks):
        raise ValueError("Block excerpt must not be blank")

    def refs(rows):
        return [{"claim_id" if isinstance(row, BriefClaim) else "block_id": row.id,
                 "source_id": row.source_id, "pdf_page": row.pdf_page,
                 "excerpt": row.excerpt} for row in rows]

    def number(value):
        return int(value) if value == value.to_integral_value() else float(value)

    def decimal(claim):
        return Decimal(str(claim.value))

    groups = defaultdict(list)
    for claim in brief.claims:
        groups[(claim.scope, claim.field)].append(claim)
    selected = {}
    conflicts, checks, missing, review_items = [], [], [], []
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
        item = {"code": code, "scope": scope, "formula": formula, "unit": unit,
                "calculated": number(calculated), "refs": refs(rows)}
        if expected is not None:
            difference = calculated - decimal(expected)
            item.update(declared=expected.value, difference=number(difference),
                        status="match" if difference == 0 else "mismatch")
            item["refs"] += refs(groups[(expected.scope, expected.field)])
            if difference != 0:
                conflicts.append({**item, "message": "明确输入的数量／名牌算术与该声明不一致。"})
        else:
            item["status"] = "calculated"
        checks.append(item)

    required = {
        "module_count": "确认全项目采用的组件总数。",
        "module_power_w": "提供精确组件名牌功率。",
        "modules_per_string": "确认整串串联块数。",
        "inverter_count": "确认逆变器总台数。",
        "inverter_power_kw": "提供单台逆变器名义AC功率。",
        "pan_file": "取得精确身份、完整曲线及合法来源的真实PAN；文件声明不证明其存在或认证。",
        "ond_file": "取得精确身份、完整曲线及合法来源的真实OND；文件声明不证明其存在或认证。",
        "hourly_weather_file": "提供具体年份、标准时间基准及来源的完整逐时天气资产。",
    }
    for field, action in required.items():
        if get(field) is None:
            missing.append({"field": field, "scope": "project",
                            "reason": "conflicting" if ("project", field) in groups else "not_declared",
                            "action": action, "refs": refs(groups.get(("project", field), []))})

    modules, power, series = get("module_count"), get("module_power_w"), get("modules_per_string")
    inverters, ac_power = get("inverter_count"), get("inverter_power_kw")
    if modules and power:
        comparison("module_dc_capacity", decimal(modules) * decimal(power) / 1000,
                   get("dc_capacity_kw"), "module_count * module_power_w / 1000",
                   [modules, power], unit="kW")
    if inverters and ac_power:
        comparison("inverter_ac_capacity", decimal(inverters) * decimal(ac_power),
                   get("ac_capacity_kw"), "inverter_count * inverter_power_kw",
                   [inverters, ac_power], unit="kW")
    if modules and series:
        whole, remainder = divmod(modules.value, series.value)
        item = {"code": "whole_strings", "scope": "project",
                "formula": "divmod(module_count, modules_per_string)", "full_strings": whole,
                "remainder_modules": remainder, "refs": refs([modules, series]),
                "status": "match" if remainder == 0 else "mismatch"}
        checks.append(item)
        if remainder:
            conflicts.append({**item, "message": "组件总数不能组成整数整串；不自动增减组件。"})
        else:
            comparison("string_count", Decimal(whole), get("string_count"),
                       "module_count / modules_per_string", [modules, series])
    if modules and power and inverters and ac_power:
        checks.append({"code": "nominal_dc_ac_ratio", "scope": "project",
                       "formula": "module_count * module_power_w / 1000 / (inverter_count * inverter_power_kw)",
                       "value": number(decimal(modules) * decimal(power) / 1000
                                       / (decimal(inverters) * decimal(ac_power))),
                       "refs": refs([modules, power, inverters, ac_power]),
                       "meaning": "名牌算术比，不是允许上限或电气校核。"})

    for scope in sorted({claim.scope for claim in brief.claims}):
        pmax, vmp, imp = (get(field, scope) for field in ("module_power_w", "module_vmp_v", "module_imp_a"))
        if pmax and vmp and imp:
            product = decimal(vmp) * decimal(imp)
            difference = decimal(pmax) - product
            item = {"code": "module_nameplate_product", "scope": scope,
                    "formula": "module_power_w - module_vmp_v * module_imp_a", "unit": "W",
                    "declared_pmax": pmax.value, "vmp_times_imp": number(product),
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
        comparison("block_count", Decimal(block_count), get("block_count"),
                   "sum(block.count)", brief.blocks)
        for attribute, expected, code in (
                ("strings_per_block", get("string_count"), "block_string_count"),
                ("inverters_per_block", inverters, "block_inverter_count")):
            absent = [block for block in brief.blocks if getattr(block, attribute) is None]
            if absent:
                missing.append({"field": f"blocks.{attribute}", "scope": "project",
                                "reason": "incomplete_block_schedule",
                                "action": "补齐每种分块的实际整数数量，不从输入路数推断。",
                                "refs": refs(absent)})
                continue
            total = sum(block.count * getattr(block, attribute) for block in brief.blocks)
            comparison(code, Decimal(total), expected,
                       f"sum(block.count * block.{attribute})", brief.blocks)
            if attribute == "strings_per_block" and series:
                comparison("block_module_count", Decimal(total) * decimal(series), modules,
                           "sum(block.count * block.strings_per_block) * modules_per_string",
                           [*brief.blocks, series])
        if block_count > 1:
            review_items.append({"code": "multiple_blocks", "scope": "project",
                "message": "分块算术不构成原生拓扑；当前统一新建路径限均匀单子阵，需实际分块接线／原生工程。",
                "refs": refs(brief.blocks)})

    storage_power, storage_energy = get("storage_power_kw"), get("storage_energy_kwh")
    if any(("project", field) in groups for field in
           ("storage_power_kw", "storage_energy_kwh", "storage_strategy")):
        for field, action in (
                ("storage_power_kw", "明确储能功率额定对象、AC/DC侧及原生设备依据。"),
                ("storage_energy_kwh", "明确名义／可用容量及原生电池硬件依据。"),
                ("storage_strategy", "明确原生支持策略；不自动生成充放电或电价调度。")):
            if get(field) is None:
                missing.append({"field": field, "scope": "project",
                                "reason": "conflicting" if ("project", field) in groups else "not_declared",
                                "action": action, "refs": refs(groups.get(("project", field), []))})
    if storage_power and storage_energy:
        checks.append({"code": "storage_nominal_hours", "scope": "project",
            "formula": "storage_energy_kwh / storage_power_kw",
            "value": number(decimal(storage_energy) / decimal(storage_power)), "unit": "h",
            "refs": refs([storage_energy, storage_power]),
            "meaning": "名义能量／功率比，不是可用时长、调度或储能物理模型。"})
        review_items.append({"code": "storage_basis", "scope": "project",
            "message": "需真实BTR／PCS、SOC、效率、温度、SOW、连接与策略；不由MW/MWh推断。",
            "refs": refs([storage_power, storage_energy]),
            "action": "先确认用途和原生支持策略，再准备对应负荷／可用性及硬件依据。"})

    next_actions = [item["action"] for item in missing]
    if conflicts:
        next_actions.insert(0, "逐项解决引用冲突和整数／容量差异，明确采信值；不自动选章节或凑整。")
    next_actions += [item["action"] for item in review_items if "action" in item]
    next_actions.append("核对原文及工程依据；精确资产声明和数量一致后仍需受支持的原生工程准备／运行验证。")
    return {
        "operation": "engineering_brief_review", "schema_version": 1,
        "status": "blocked" if conflicts or missing else "needs_review",
        "readonly": True, "simulated": False, "native_consumption_verified": False,
        "engineering_certified": False, "source_quotes_verified": False,
        "sources": [source.model_dump() for source in brief.sources],
        "facts": [claim.model_dump() for claim in brief.claims],
        "blocks": [block.model_dump() for block in brief.blocks],
        "checks": checks, "conflicts": conflicts, "missing": missing,
        "review_items": review_items, "next_actions": list(dict.fromkeys(next_actions)),
        "limitations": [
            "只检查调用方结构化声明及十进制数量算术；不读取PDF、文件或网络，不验证引用真实性。",
            "不选择厂家型号、拟合模型、校核温度／电气包络、重建拓扑、算遮阴或储能调度。",
            "资产名称仅是声明，未检查文件存在、完整模型或逐时覆盖；一致输入仍需工程复核及原生验证。",
            "nominal_capacity_kw是摘要口径，不与实际dc_capacity_kw自动等同；不同scope不互相覆盖。",
        ],
    }
