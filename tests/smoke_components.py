"""Real MCP stdio component lifecycle in an isolated temporary workspace."""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_components import PAN, OND, BTR, GEN
from test_component_editor import CURVED_OND, POINTS
from test_battery_editor import CURVED_BTR, CAPACITY_POINTS
from test_pan_gui_format import GUI_PAN
from test_variants import VARIANT


async def main():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "workspace"
        root.mkdir()
        (root / "Projects").mkdir()
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / "pvsyst_mcp_server.py")],
            env={**os.environ, "PVSYST_CLI": sys.executable,
                 "PVSYST_WORKSPACE": str(root)},
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 45

                async def call(name, args, expect_error=False):
                    result = await session.call_tool(name, args)
                    assert result.is_error == expect_error, (name, result)
                    return result

                async def data_call(name, args):
                    result = await call(name, args)
                    return json.loads(result.content[0].text)

                await data_call("pvsyst_create_component", {
                    "component_type": "OND", "filename": "curve.OND", "content": CURVED_OND,
                })
                inspected = await data_call("pvsyst_get_component", {
                    "component_type": "OND", "filename": "curve.OND",
                })
                assert inspected["curves"]["items"][0]["points"] == POINTS
                assert inspected["curves"]["control"]["source"] == "automatic"
                curve_args = {
                    "component_type": "OND", "filename": "curve.OND",
                    "expected_sha256": inspected["sha256"],
                    "updates": {"PNomConv": "45"},
                    "curve_updates": {"Converter/ProfilPIO": [[x, y * 0.9] for x, y in POINTS]},
                    "use_file_curve": True,
                }
                await call("pvsyst_update_component", {**curve_args, "use_file_curve": False}, expect_error=True)
                await call("pvsyst_update_component", {**curve_args,
                    "curve_updates": {"Converter/ProfilPIO": [[True, 0]] + POINTS[1:]}}, expect_error=True)
                await call("pvsyst_update_component", {**curve_args,
                    "curve_updates": {"Converter/ProfilPIO": [["100", 0]] + POINTS[1:]}}, expect_error=True)
                preview = await data_call("pvsyst_update_component", {**curve_args, "dry_run": True})
                assert preview["curve_control_after"]["source"] == "file"
                curve_file = root / "ComposPV" / "Inverters" / "curve.OND"
                assert hashlib.sha256(curve_file.read_bytes()).hexdigest() == inspected["sha256"]
                assert not (root / "ComposPV" / ".mcp-backups").exists()
                edited = await data_call("pvsyst_update_component", curve_args)
                assert edited["sha256"] == preview["sha256"]
                await call("pvsyst_update_component", curve_args, expect_error=True)
                restored = await data_call("pvsyst_restore_component", {
                    "component_type": "OND", "filename": "curve.OND",
                    "backup_name": edited["backup_name"], "confirm": True,
                })
                assert restored["sha256"] == inspected["sha256"]
                print("MCP component edit: auto guard, strict numbers, scalar + curve preview/apply, stale rejection, exact restore: OK")

                for kind, content in (("PAN", PAN), ("OND", OND), ("BTR", BTR), ("GEN", GEN)):
                    target = {"component_type": kind, "filename": f"commercial.{kind}"}
                    created = await data_call("pvsyst_create_component", {**target, "content": content})
                    info = await data_call("pvsyst_get_component", target)
                    assert info["commercial"]["editable"]
                    args = {**target, "updates": {}, "expected_sha256": info["sha256"],
                        "commercial_updates": {"Model": "Commercial edit", "Width": "1.2",
                            "Height": "2.1", "Weight": "26", "YearBeg": "2026"},
                        "remarks": ["Editable form", "Second line"]}
                    await call("pvsyst_update_component", {**args, "expected_sha256": None}, expect_error=True)
                    preview = await data_call("pvsyst_update_component", {**args, "dry_run": True})
                    assert (await data_call("pvsyst_get_component", target))["sha256"] == info["sha256"]
                    edited = await data_call("pvsyst_update_component", args)
                    assert edited["sha256"] == preview["sha256"]
                    assert edited["commercial_after"]["remarks"] == args["remarks"]
                    if kind == "PAN":
                        inspected = await data_call("pvsyst_get_component", target)
                        assert inspected["fields"]["LargApp"] == ["1.2"]
                        assert inspected["fields"]["LongApp"] == ["2.1"]
                    await call("pvsyst_update_component", args, expect_error=True)
                    restored = await data_call("pvsyst_restore_component", {
                        **target, "backup_name": edited["backup_name"], "confirm": True})
                    assert restored["sha256"] == created["sha256"]
                print("MCP commercial forms: four types, field insertion, remarks, PAN dimensions, preview/apply, stale guard and exact restore: OK")

                gui_target = {"component_type": "PAN", "filename": "gui.PAN"}
                gui_created = await data_call("pvsyst_create_component", {**gui_target, "content": GUI_PAN})
                gui_info = await data_call("pvsyst_get_component", gui_target)
                assert not gui_info["warnings"] and "Isc" in gui_info["editable_fields"]
                assert gui_info["commercial"]["fields"]["Width"]["editable"]
                assert not gui_info["commercial"]["fields"]["Width"]["paired_root_present"]
                gui_args = {**gui_target, "expected_sha256": gui_info["sha256"],
                            "updates": {"ISC": "11.2", "MuISC": "0.006"},
                            "commercial_updates": {"Width": "1.05", "Height": "2.0"}}
                gui_preview = await data_call("pvsyst_update_component", {**gui_args, "dry_run": True})
                gui_edited = await data_call("pvsyst_update_component", gui_args)
                assert gui_preview["sha256"] == gui_edited["sha256"]
                gui_fields = (await data_call("pvsyst_get_component", gui_target))["fields"]
                assert gui_fields["Isc"] == ["11.2"] and gui_fields["muISC"] == ["0.006"]
                assert "LargApp" not in gui_fields and "LongApp" not in gui_fields
                await call("pvsyst_update_component", gui_args, expect_error=True)
                gui_restored = await data_call("pvsyst_restore_component", {**gui_target,
                    "backup_name": gui_edited["backup_name"], "confirm": True})
                assert gui_restored["sha256"] == gui_created["sha256"]
                print("MCP GUI PAN: create/inspect, scoped aliases, commercial-only dimensions, preview, stale guard and exact restore: OK")

                battery_target = {"component_type": "BTR", "filename": "capacity.BTR"}
                await data_call("pvsyst_create_component", {**battery_target, "content": CURVED_BTR})
                battery = await data_call("pvsyst_get_component", battery_target)
                assert battery["curves"]["control"]["source"] == "unrecognized-tag"
                battery_args = {**battery_target, "updates": {}, "expected_sha256": battery["sha256"],
                    "curve_updates": {"Capa_DischRate": [[x, y * 0.9] for x, y in CAPACITY_POINTS]},
                    "use_file_curve": True}
                await call("pvsyst_update_component", {**battery_args, "use_file_curve": False}, expect_error=True)
                await call("pvsyst_update_component", {**battery_args,
                    "curve_updates": {"Capa_DischRate": [[x, y * 0.8] for x, y in CAPACITY_POINTS]}}, expect_error=True)
                battery_preview = await data_call("pvsyst_update_component", {**battery_args, "dry_run": True})
                battery_path = root / "ComposPV" / "Batteries" / "capacity.BTR"
                assert hashlib.sha256(battery_path.read_bytes()).hexdigest() == battery["sha256"]
                battery_edit = await data_call("pvsyst_update_component", battery_args)
                assert battery_edit["sha256"] == battery_preview["sha256"]
                inspected_battery = await data_call("pvsyst_get_component", battery_target)
                assert inspected_battery["curves"]["control"]["path"] == "CapaCourant"
                assert abs(inspected_battery["curves"]["items"][1]["ratio_100h"] - 1.17) < 1e-10
                await call("pvsyst_update_component", battery_args, expect_error=True)
                battery_restore = await data_call("pvsyst_restore_component", {**battery_target,
                    "backup_name": battery_edit["backup_name"], "confirm": True})
                assert battery_restore["sha256"] == battery["sha256"]
                print("MCP BTR curve: ignored-tag guard, ratio rejection, activation preview/apply, stale rejection, exact restore: OK")

                for kind, filename, points, curve in (
                    ("OND", "curve.OND", POINTS, "Converter/ProfilPIO"),
                    ("BTR", "capacity.BTR", CAPACITY_POINTS, "Capa_DischRate")):
                    target = {"component_type": kind, "filename": filename}
                    original = await data_call("pvsyst_get_component", target)
                    current_hash = original["sha256"]
                    more = points + ([[1500, 1425]] if kind == "OND" else [[300, 1.44]])
                    fewer = [more[0], more[1], more[3], more[-1]]
                    first_backup = None
                    for supplied in (more, fewer):
                        args = {**target, "updates": {}, "expected_sha256": current_hash,
                                "use_file_curve": True, "curve_updates": {curve: supplied}}
                        preview = await data_call("pvsyst_update_component", {**args, "dry_run": True})
                        assert (await data_call("pvsyst_get_component", target))["sha256"] == current_hash
                        edit = await data_call("pvsyst_update_component", args)
                        first_backup = first_backup or edit["backup_name"]
                        assert edit["sha256"] == preview["sha256"]
                        await call("pvsyst_update_component", args, expect_error=True)
                        current_hash = edit["sha256"]
                        curve = "CapaCourant" if kind == "BTR" else curve
                        item = next(c for c in (await data_call("pvsyst_get_component", target))["curves"]["items"]
                                    if c["path"] == curve)
                        assert item["point_count_editable"] and item["points"] == supplied
                        assert item["point_count"] == item["allocated_point_count"] == len(supplied)
                        noop = await data_call("pvsyst_update_component", {**args,
                            "expected_sha256": current_hash, "curve_updates": {curve: supplied}})
                        assert not noop["changed"] and not noop.get("backup_name")
                    restored = await data_call("pvsyst_restore_component", {**target,
                        "backup_name": first_backup, "confirm": True})
                    assert restored["sha256"] == original["sha256"]
                print("MCP OND/BTR point tables: grow/shrink, synchronized counts, previews, stale guard, no-op and exact restore: OK")

                for kind, filename, points, curve in (
                    ("OND", "curve.OND", POINTS, "Converter/ProfilPIO"),
                    ("BTR", "capacity.BTR", CAPACITY_POINTS, "Capa_DischRate")):
                    target = {"component_type": kind, "filename": filename}
                    original = await data_call("pvsyst_get_component", {**target, "include_curve_csv": True})
                    await call("pvsyst_get_component", {**target, "include_curve_csv": "true"}, expect_error=True)
                    exported = original["curve_csv"][curve]
                    header = exported.splitlines()[0]
                    more = points + ([[1500, 1425]] if kind == "OND" else [[300, 1.44]])
                    text = header + "\r\n" + "".join(f"{x},{y}\r\n" for x, y in more)
                    args = {**target, "updates": {}, "expected_sha256": original["sha256"],
                            "use_file_curve": True}
                    await call("pvsyst_update_component", {**args, "curve_csv": {curve: 123}}, expect_error=True)
                    await call("pvsyst_update_component", {**args, "curve_csv": {curve: text},
                        "curve_updates": {curve: more}}, expect_error=True)
                    array_preview = await data_call("pvsyst_update_component", {**args,
                        "curve_updates": {curve: more}, "dry_run": True})
                    preview = await data_call("pvsyst_update_component", {**args,
                        "curve_csv": {curve: text}, "dry_run": True})
                    assert preview == array_preview
                    assert (await data_call("pvsyst_get_component", target))["sha256"] == original["sha256"]
                    changed = await data_call("pvsyst_update_component", {**args, "curve_csv": {curve: text}})
                    assert changed["sha256"] == preview["sha256"]
                    await call("pvsyst_update_component", {**args, "curve_csv": {curve: text}}, expect_error=True)
                    current = await data_call("pvsyst_get_component", {**target, "include_curve_csv": True})
                    noop = await data_call("pvsyst_update_component", {**args,
                        "expected_sha256": current["sha256"], "curve_csv": current["curve_csv"]})
                    assert not noop["changed"] and not noop.get("backup_name")
                    restored = await data_call("pvsyst_restore_component", {**target,
                        "backup_name": changed["backup_name"], "confirm": True})
                    assert restored["sha256"] == original["sha256"]
                print("MCP OND/BTR CSV: export, strict input, equivalent preview, growth, stale guard, round-trip no-op and exact restore: OK")

                for kind, content in (("PAN", PAN), ("OND", OND),
                                      ("BTR", BTR), ("GEN", GEN)):
                    await call("pvsyst_create_component", {
                        "component_type": kind, "filename": f"created.{kind}",
                        "content": content,
                    })
                    await call("pvsyst_validate_component", {
                        "component_type": kind, "filename": f"created.{kind}",
                    })
                await call("pvsyst_list_components", {"component_type": "BTR"})
                await call("pvsyst_get_component", {
                    "component_type": "PAN", "filename": "created.PAN", "limit": 2,
                })
                edited_component = await data_call("pvsyst_update_component", {
                    "component_type": "GEN", "filename": "created.GEN",
                    "updates": {"Model": "Updated generator"},
                })
                rejected = await call("pvsyst_update_component", {
                    "component_type": "GEN", "filename": "created.GEN",
                    "updates": {"CFuelHor": "0"},
                }, expect_error=True)
                assert "CFuelHor must be greater than zero" in rejected.content[0].text
                assert "Model=Updated generator" in (
                    root / "ComposPV" / "Gensets" / "created.GEN").read_text(encoding="utf-8")
                await call("pvsyst_backup_component", {
                    "component_type": "GEN", "filename": "created.GEN",
                })
                restored_component = await data_call("pvsyst_restore_component", {
                    "component_type": "GEN", "filename": "created.GEN",
                    "backup_name": edited_component["backup_name"], "confirm": True,
                })
                assert restored_component["integrity_verified"]
                await call("pvsyst_clone_component", {
                    "component_type": "GEN", "source_name": "created.GEN",
                    "new_name": "clone.GEN",
                    "updates": {"Manufacturer": "Example", "Model": "Clone"},
                })
                await call("pvsyst_compare_components", {
                    "component_type": "GEN", "first": "created.GEN", "second": "clone.GEN",
                })
                archived_component = await data_call("pvsyst_archive_component", {
                    "component_type": "GEN", "filename": "clone.GEN",
                    "confirm": True,
                })
                restored_component = await data_call("pvsyst_restore_archived_component", {
                    "component_type": "GEN", "filename": "clone.GEN",
                    "archive_name": archived_component["archive_name"], "confirm": True,
                })
                assert restored_component["integrity_verified"]
                await call("pvsyst_copy_component", {
                    "component_type": "OND", "source_name": "created.OND",
                    "new_name": "copy.OND",
                })
                print("MCP component lifecycle: 4 creates, validate, read, edit, rejected GEN zero, clone, diff, archive/restore, copy: OK")
                (root / "Projects" / "Example.PRJ").write_text(
                    "PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
                (root / "Projects" / "Example.VC0").write_text(VARIANT, encoding="utf-8")
                await call("pvsyst_get_variant_components", {"project": "Example.PRJ", "variant": "VC0"})
                await call("pvsyst_clone_variant", {
                    "project": "Example.PRJ", "source_variant": "VC0", "new_variant": "VC1",
                    "updates": {"PAN": "created.PAN"}, "subarray_id": 2,
                })
                target = root / "Projects" / "Example.VC1"
                before_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                await call("pvsyst_update_variant_components", {
                    "project": "Example.PRJ", "variant": "VC1",
                    "updates": {"OND": "copy.OND"}, "expected_sha256": before_hash,
                })
                after_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                variant_backup = next((root / "Projects" / ".mcp-variant-backups").iterdir()).name
                await call("pvsyst_restore_variant", {
                    "project": "Example.PRJ", "variant": "VC1", "backup_name": variant_backup,
                    "expected_sha256": after_hash, "confirm": True,
                })
                assert hashlib.sha256(target.read_bytes()).hexdigest() == before_hash
                print("MCP variant lifecycle: inspect, scoped clone, guarded edit and restore: OK")

                (root / "Sites").mkdir()
                (root / "Meteo").mkdir()
                (root / "Sites" / "New.SIT").write_text(
                    "PVObject_=pvSite\n  NomF=New.SIT\n  Site=New Site\n"
                    "End of PVObject pvSite\n", encoding="utf-8")
                (root / "Meteo" / "New.MET").write_bytes(b"offline fixture")
                (root / "Projects" / "Example.PRJ").write_text(
                    "PVObject_=pvProject\nMeteoFileName=Old.MET\n"
                    "PVObject_SitePrj=pvSite\n  NomF=Old.SIT\n"
                    "End of PVObject pvSite\nEnd of PVObject pvProject\n", encoding="utf-8")
                for path in (root / "Projects").glob("Example.VC*"):
                    variant = path.read_text(encoding="utf-8").replace("  PVObject_=pvOrient\n",
                        "  PVObject_SiteSimul=pvSite\n    NomF=Old.SIT\n"
                        "  End of PVObject pvSite\n  PVObject_MeteoSimul=pvMeteo\n"
                        "    NomF=Old.MET\n    SiteM=Old Site\n"
                        "    PVObject_SiteMet=pvSite\n      NomF=Old.SIT\n"
                        "    End of PVObject pvSite\n  End of PVObject pvMeteo\n"
                        "  PVObject_=pvOrient\n")
                    path.write_text(variant, encoding="utf-8")
                await call("pvsyst_clone_project", {"source_project": "Example.PRJ", "new_project": "Copy.PRJ"})
                initial = await data_call("pvsyst_inspect_project", {"project": "Copy.PRJ"})
                changed = await data_call("pvsyst_update_project_sources", {
                    "project": "Copy.PRJ", "site_name": "New.SIT", "met_name": "New.MET",
                    "expected_files": initial["files"],
                })
                inspected = await data_call("pvsyst_inspect_project", {"project": "Copy.PRJ"})
                assert inspected["files"] == changed["files"] and not inspected["warnings"]
                restored = await data_call("pvsyst_restore_project_sources", {
                    "project": "Copy.PRJ", "backups": changed["backups"],
                    "expected_files": changed["files"], "confirm": True,
                })
                assert restored["files"] == initial["files"]
                parameters = await data_call("pvsyst_get_variant_parameters", {"project": "Copy.PRJ", "variant": "VC0"})
                edited = await data_call("pvsyst_update_variant_parameters", {
                    "project": "Copy.PRJ", "variant": "VC0", "expected_sha256": parameters["sha256"],
                    "subarray_updates": {"NModSerie": 15}, "orientation_updates": {"FieldTilt": 25},
                })
                await call("pvsyst_validate_variant_structure", {"project": "Copy.PRJ", "variant": "VC0"})
                await call("pvsyst_restore_variant", {
                    "project": "Copy.PRJ", "variant": "VC0", "expected_sha256": edited["sha256"],
                    "backup_name": edited["backup_name"], "confirm": True,
                })
                archived = await data_call("pvsyst_archive_variant", {
                    "project": "Copy.PRJ", "variant": "VC1", "expected_sha256": initial["files"]["Copy.VC1"],
                    "confirm": True,
                })
                await call("pvsyst_restore_project_archive", {
                    "project": "Copy.PRJ", "archive_name": archived["archive_name"], "confirm": True,
                })
                archived = await data_call("pvsyst_archive_project", {
                    "project": "Copy.PRJ", "expected_files": initial["files"], "confirm": True,
                })
                await call("pvsyst_restore_project_archive", {
                    "project": "Copy.PRJ", "archive_name": archived["archive_name"], "confirm": True,
                })
                final = await data_call("pvsyst_inspect_project", {"project": "Copy.PRJ"})
                assert final["files"] == initial["files"]
                print("MCP project lifecycle: copy, source update/restore, parameters, variant/project archive and restore: OK")
                target = {"project": "Copy.PRJ", "variant": "VC0"}
                inspected = await data_call("pvsyst_get_variant_parameters", target)
                args = {**target, "expected_sha256": inspected["sha256"], "generator_updates": {
                    "enabled": True, "filename": "created.GEN", "operating_power_kw": 40,
                    "thresholds": {"1": {"VBkUpEncl_syst": 0.9, "VBkUpDecl_syst": 0.95},
                                   "2": {"VBkUpEncl_syst": 0.3, "VBkUpDecl_syst": 0.5}}}}
                await call("pvsyst_update_variant_parameters", {**args, "generator_updates": {"enabled": "yes"}}, expect_error=True)
                await call("pvsyst_update_variant_parameters", {**args, "generator_updates": {"operating_power_kw": "40"}}, expect_error=True)
                preview = await data_call("pvsyst_update_variant_parameters", {**args, "dry_run": True})
                assert (await data_call("pvsyst_get_variant_parameters", target))["sha256"] == inspected["sha256"]
                changed = await data_call("pvsyst_update_variant_parameters", args)
                assert changed["sha256"] == preview["sha256"]
                assert changed["generator_after"]["enabled"] and changed["generator_after"]["operating_power_kw"] == 40
                await call("pvsyst_update_variant_parameters", args, expect_error=True)
                disabled = await data_call("pvsyst_update_variant_parameters", {
                    **target, "expected_sha256": changed["sha256"], "generator_updates": {"enabled": False}})
                assert not disabled["generator_after"]["enabled"]
                restored = await data_call("pvsyst_restore_variant", {**target, "expected_sha256": disabled["sha256"],
                    "backup_name": changed["backup_name"], "confirm": True})
                assert restored["sha256"] == inspected["sha256"]
                print("MCP generator: typed settings, multi-subarray thresholds, preview, enable/disable, stale guard, exact restore: OK")


if __name__ == "__main__":
    asyncio.run(main())
