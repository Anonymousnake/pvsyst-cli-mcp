"""Public cited-brief contract; structured report facts only, no native or model fixture."""
import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


REPO = Path(__file__).resolve().parents[2]


def report_brief():
    """Small public-boundary fixture from PDF physical pages 14, 15, 105 and 108."""
    source = {"id": "report", "title": "龙口120MW农光互补可研摘录",
              "locator": "caller-provided PDF; physical pages 14, 15, 105, 108"}
    quantities = "共218690 块，纵向竖排方阵倾角32°，每个光伏组串由26 块光伏组件串联而成。"
    inverter = "拟安装320 台额定容量300kW 组串式逆变器"

    def claim(id_, field, value, unit, excerpt, page=15):
        return {"id": id_, "field": field, "scope": "project", "value": value,
                "unit": unit, "source_id": "report", "pdf_page": page, "excerpt": excerpt}

    return {"sources": [source], "claims": [
        claim("modules", "module_count", 218690, "count", quantities),
        claim("series", "modules_per_string", 26, "count", quantities),
        claim("pmax", "module_power_w", 550, "W", "最大功率（Pmax）\n550Wp"),
        claim("vmp", "module_vmp_v", 41.95, "V", "峰值功率电压（Vmp）\n41.95V"),
        claim("imp", "module_imp_a", 13.35, "A", "峰值功率电流（Imp）\n13.35A"),
        claim("inverters", "inverter_count", 320, "count", inverter),
        claim("ac_unit", "inverter_power_kw", 300, "kW", inverter),
        claim("blocks", "block_count", 34, "count", "项目共有34 个光伏发电单元", 14),
        claim("inputs", "inverter_input_count", 24, "count",
              "逆变器的直流输入最多为24路，容配比为1.1", 108),
    ], "blocks": [
        {"id": "3.15-unit", "count": 24, "strings_per_block": 270,
         "inverters_per_block": 10, "source_id": "report", "pdf_page": 105,
         "excerpt": "每个发电单元共有270 个光伏组串，配置10 台逆变器；1 台\n3150kVA 升压变压器。共有24 个3.15MW 光伏发电单元。"},
        {"id": "2.5-unit", "count": 10, "strings_per_block": 208,
         "inverters_per_block": 8, "source_id": "report", "pdf_page": 105,
         "excerpt": "每个发电单元共有208 个光伏组串，配置8 台逆变器，1\n台2500kVA 升压变压器。共有10 个2.5MW 光伏发电单元。"},
    ]}


class EngineeringBriefBoundary(unittest.TestCase):
    def test_public_brief_aggregation_and_readonly_boundary(self):
        asyncio.run(self.check())

    async def check(self):
        directory = Path(tempfile.mkdtemp(prefix="pvsyst-engineering-brief-"))
        try:
            workspace = directory / "PVsyst8.1_Data"
            workspace.mkdir()
            original = workspace / "keep.txt"
            original.write_text("Public brief must preserve this file.\n", encoding="utf-8")
            executable = directory / "PVsystCLI.exe"
            executable.write_bytes(b"Not an executable; brief review must never launch native software")
            parameters = StdioServerParameters(command=sys.executable,
                args=["-B", str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO),
                env=dict(os.environ, PVSYST_WORKSPACE=str(workspace), PVSYST_CLI=str(executable)))
            members = set(directory.rglob("*"))
            original_bytes = original.read_bytes()
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    tool = next(item for item in tools.tools
                                if item.name == "pvsyst_review_engineering_brief")
                    self.assertTrue(tool.description.startswith("[Engineering]"))

                    async def review(brief, **extra):
                        reply = await session.call_tool("pvsyst_review_engineering_brief",
                                                        {"brief": brief, **extra})
                        return reply, (None if reply.is_error else json.loads("\n".join(
                            item.text for item in reply.content if hasattr(item, "text"))))

                    brief = report_brief()
                    before = copy.deepcopy(brief)
                    reply, result = await review(brief)
                    self.assertFalse(reply.is_error, reply.content)
                    self.assertEqual(result["status"], "blocked")
                    self.assertEqual(brief, before)
                    checks = {item["code"]: item for item in result["checks"]}
                    self.assertEqual(checks["whole_strings"]["full_strings"], 8411)
                    self.assertEqual(checks["whole_strings"]["remainder_modules"], 4)
                    self.assertEqual(checks["module_dc_capacity"]["calculated"], 120279.5)
                    self.assertEqual(checks["block_string_count"]["calculated"], 8560)
                    self.assertEqual(checks["block_module_count"]["calculated"], 222560)
                    self.assertEqual(checks["block_module_count"]["difference"], 3870)
                    self.assertEqual(checks["block_inverter_count"]["calculated"], 320)
                    self.assertEqual(checks["module_nameplate_product"]["vmp_times_imp"], 560.0325)
                    self.assertEqual(checks["module_nameplate_product"]["difference"], -10.0325)
                    self.assertTrue({"pan_file", "ond_file", "hourly_weather_file"}.issubset(
                        {item["field"] for item in result["missing"]}))
                    self.assertTrue({"whole_strings", "block_module_count"}.issubset(
                        {item["code"] for item in result["conflicts"]}))
                    self.assertTrue(any(item["code"] == "input_allocation_unresolved"
                                        for item in result["review_items"]))
                    self.assertTrue(all(item["refs"] for item in result["conflicts"]))
                    self.assertTrue(result["readonly"])
                    for key in ("simulated", "native_consumption_verified",
                                "engineering_certified", "source_quotes_verified"):
                        self.assertFalse(result[key])

                    contradictory = copy.deepcopy(brief)
                    contradictory["sources"].append({"id": "deliberate-conflict",
                        "title": "Deliberately conflicting contract declaration",
                        "locator": "Contract input only; not an assertion about report text"})
                    variant = copy.deepcopy(contradictory["claims"][0])
                    variant.update(id="different-declaration", value=222560, pdf_page=1,
                                   source_id="deliberate-conflict",
                                   excerpt="Contract-only conflicting module_count=222560")
                    contradictory["claims"].append(variant)
                    reply, conflict = await review(contradictory)
                    self.assertFalse(reply.is_error, reply.content)
                    self.assertTrue(any(item["code"] == "conflicting_claims"
                                        and item["field"] == "module_count"
                                        for item in conflict["conflicts"]))
                    self.assertNotIn("module_dc_capacity", {item["code"] for item in conflict["checks"]})

                    consistent = {"sources": [{"id": "declared", "title": "Explicit contract declarations",
                                               "locator": "caller declarations; no external asset verification"}],
                                  "claims": [], "blocks": []}
                    for index, (field, value, unit) in enumerate((
                            ("module_count", 52, "count"), ("modules_per_string", 26, "count"),
                            ("module_power_w", 550, "W"), ("inverter_count", 1, "count"),
                            ("inverter_power_kw", 30, "kW"), ("pan_file", "Declared.PAN", "file_name"),
                            ("ond_file", "Declared.OND", "file_name"),
                            ("hourly_weather_file", "Declared.MET", "file_name"))):
                        consistent["claims"].append({"id": f"declared-{index}", "field": field,
                            "scope": "project", "value": value, "unit": unit, "source_id": "declared",
                            "pdf_page": 1, "excerpt": f"Explicit caller declaration: {field}={value} {unit}"})
                    reply, reviewed = await review(consistent)
                    self.assertFalse(reply.is_error, reply.content)
                    self.assertEqual(reviewed["status"], "needs_review")
                    self.assertEqual(reviewed["conflicts"], [])
                    self.assertEqual(reviewed["missing"], [])
                    self.assertFalse(reviewed["engineering_certified"])
                    self.assertFalse(reviewed["source_quotes_verified"])

                    # Same quantity in a distinct scenario is preserved, not a project override.
                    other_scope = copy.deepcopy(consistent)
                    other = copy.deepcopy(other_scope["claims"][0])
                    other.update(id="other-scenario", scope="alternative", value=104,
                                 excerpt="Explicit caller declaration: module_count=104 count")
                    other_scope["claims"].append(other)
                    reply, scoped = await review(other_scope)
                    self.assertFalse(reply.is_error, reply.content)
                    self.assertEqual(scoped["conflicts"], [])
                    self.assertEqual(scoped["status"], "needs_review")

                    invalid = []
                    for change in ("bool", "numeric_string", "float_count", "unit", "extra",
                                   "unknown_source", "page", "duplicate", "unknown_field", "blank"):
                        bad = copy.deepcopy(brief)
                        first = bad["claims"][0]
                        if change == "bool":
                            first["value"] = True
                        elif change == "numeric_string":
                            first["value"] = "218690"
                        elif change == "float_count":
                            first["value"] = 218690.0
                        elif change == "unit":
                            first["unit"] = "MW"
                        elif change == "extra":
                            first["certified"] = True
                        elif change == "unknown_source":
                            first["source_id"] = "absent"
                        elif change == "page":
                            first["pdf_page"] = 0
                        elif change == "duplicate":
                            bad["claims"].append(copy.deepcopy(first))
                        elif change == "unknown_field":
                            first["field"] = "temperature_default"
                        else:
                            first["excerpt"] = " "
                        invalid.append((change, bad))
                    for name, bad in invalid:
                        with self.subTest(invalid=name):
                            reply, _ = await review(bad)
                            self.assertTrue(reply.is_error, reply.content)
                    reply, _ = await review(brief, dry_run=False)
                    self.assertTrue(reply.is_error, reply.content)
            self.assertEqual(set(directory.rglob("*")), members)
            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertEqual(executable.read_bytes(),
                             b"Not an executable; brief review must never launch native software")
        except BaseException:
            print(f"Engineering brief failure inputs retained: {directory}", file=sys.stderr)
            raise
        else:
            shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()
