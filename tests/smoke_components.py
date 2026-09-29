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


if __name__ == "__main__":
    asyncio.run(main())
