"""Real MCP stdio component lifecycle in an isolated temporary workspace."""
import asyncio
import hashlib
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
                assert len(tools.tools) == 33

                async def call(name, args, expect_error=False):
                    result = await session.call_tool(name, args)
                    assert result.is_error == expect_error, (name, result)
                    return result

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
                await call("pvsyst_update_component", {
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
                backup_name = next((root / "ComposPV" / ".mcp-backups" / "GEN").iterdir()).name
                await call("pvsyst_restore_component", {
                    "component_type": "GEN", "filename": "created.GEN",
                    "backup_name": backup_name, "confirm": True,
                })
                await call("pvsyst_clone_component", {
                    "component_type": "GEN", "source_name": "created.GEN",
                    "new_name": "clone.GEN",
                    "updates": {"Manufacturer": "Example", "Model": "Clone"},
                })
                await call("pvsyst_compare_components", {
                    "component_type": "GEN", "first": "created.GEN", "second": "clone.GEN",
                })
                await call("pvsyst_archive_component", {
                    "component_type": "GEN", "filename": "clone.GEN",
                    "confirm": True,
                })
                archive_dir = root / "ComposPV" / ".mcp-archive" / "GEN"
                archive_name = next(archive_dir.iterdir()).name
                await call("pvsyst_restore_archived_component", {
                    "component_type": "GEN", "filename": "clone.GEN",
                    "archive_name": archive_name, "confirm": True,
                })
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


if __name__ == "__main__":
    asyncio.run(main())
