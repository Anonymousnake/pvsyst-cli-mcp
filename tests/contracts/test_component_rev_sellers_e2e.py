"""External contract: REV (Sellers pvAddress) component family over public stdio MCP.

Exercises only the existing public tools (list/get/validate/create/copy/clone/
update/backup/restore) against real workspace files. The fixture is a minimal
generated native pvAddress text; no vendor artifact or private path is stored
here. Zero native CLI execution: a placeholder PVSYST_CLI file is never run.
"""
import asyncio
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

# Minimal generated native pvAddress record (CRLF, UTF-8, no BOM), matching the
# Sellers field layout observed in shipped 8.1.7 DataRO: Comment/Version/Flags/
# Person/Address/Town/Country. Address is optional in native saves.
FIXTURE_NAME = "Contract_Test_Retailer.REV"
FIXTURE = (
    "PVObject_=pvAddress\r\n"
    "  Comment=Contract Test Seller\r\n"
    "  Version=6.78\r\n"
    "  Flags=$80008022\r\n"
    "  Person=Alice Example\r\n"
    "  Address=1 Test Street\r\n"
    "  Town=Testville\r\n"
    "  Country=Testland\r\n"
    "End of PVObject pvAddress\r\n"
)


class RevSellersComponentContract(unittest.TestCase):
    def test_rev_sellers_component_family(self):
        asyncio.run(self.check())

    async def check(self):
        root = Path(tempfile.mkdtemp(prefix="pvsyst-rev-sellers-"))
        succeeded = False
        try:
            (root / "ComposPV" / "Sellers").mkdir(parents=True)
            (root / "Projects").mkdir()
            fixture_path = root / "ComposPV" / "Sellers" / FIXTURE_NAME
            fixture_path.write_bytes(FIXTURE.encode("utf-8"))
            original = fixture_path.read_bytes()
            fake = root / "PVsystCLI.exe"
            fake.write_bytes(b"Not an executable")
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO),
                env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(fake),
                         PVSYST_TOOL_GROUPS="components"))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        response = await session.call_tool(tool, args)
                        self.assertFalse(response.is_error, (tool, response.content))
                        return json.loads("\n".join(item.text for item in response.content
                                                    if hasattr(item, "text")))

                    async def rejected(tool, args):
                        response = await session.call_tool(tool, args)
                        self.assertTrue(response.is_error, (tool, response.content))

                    # list + validate the native kind
                    listing = await call("pvsyst_list_components", {"component_type": "REV"})
                    self.assertIn(FIXTURE_NAME, {item["name"] for item in listing["items"]})
                    validated = await call("pvsyst_validate_component",
                                           {"component_type": "REV", "filename": FIXTURE_NAME})
                    self.assertEqual(validated["format"], "text")
                    self.assertEqual(validated["structural_errors"], [])

                    # get exposes native editable text fields + honest no-curve/no-form
                    info = await call("pvsyst_get_component",
                                      {"component_type": "REV", "filename": FIXTURE_NAME})
                    self.assertEqual(info["type"], "REV")
                    self.assertEqual(info["format"], "text")
                    self.assertIsNone(info["manufacturer"])
                    self.assertIsNone(info["model"])
                    self.assertEqual(set(info["editable_fields"]),
                                     {"Comment", "Person", "Address", "Town", "Country"})
                    self.assertEqual(info["curves"]["items"], [])
                    self.assertIs(info["commercial"]["editable"], False)
                    self.assertEqual(info["fields"]["Flags"], ["$80008022"])
                    self.assertEqual(info["fields"]["Version"], ["6.78"])
                    sha0 = info["sha256"]

                    # dry run preview: no bytes change
                    preview = await call("pvsyst_update_component", {
                        "component_type": "REV", "filename": FIXTURE_NAME,
                        "updates": {"Person": "Bob Native"}, "expected_sha256": sha0,
                        "dry_run": True})
                    self.assertTrue(preview["dry_run"])
                    self.assertTrue(preview["changed"])
                    self.assertEqual(fixture_path.read_bytes(), original)

                    # guarded update: read back native fields, preserve unknown fields/EOL
                    edited = await call("pvsyst_update_component", {
                        "component_type": "REV", "filename": FIXTURE_NAME,
                        "updates": {"Person": "Bob Native", "Town": "Nativeville"},
                        "expected_sha256": sha0})
                    self.assertTrue(edited["changed"])
                    after = fixture_path.read_bytes()
                    self.assertEqual(after.count(b"\r\n"), original.count(b"\r\n"))
                    self.assertFalse(after.startswith(b"\xef\xbb\xbf"))
                    for kept in (b"Comment=Contract Test Seller\r\n", b"Version=6.78\r\n",
                                 b"Flags=$80008022\r\n", b"Address=1 Test Street\r\n",
                                 b"Country=Testland\r\n"):
                        self.assertIn(kept, after)
                    self.assertIn(b"Person=Bob Native\r\n", after)
                    self.assertIn(b"Town=Nativeville\r\n", after)
                    reread = await call("pvsyst_get_component",
                                        {"component_type": "REV", "filename": FIXTURE_NAME})
                    self.assertEqual(reread["fields"]["Person"], ["Bob Native"])
                    self.assertEqual(reread["fields"]["Town"], ["Nativeville"])
                    self.assertEqual(reread["fields"]["Flags"], ["$80008022"])

                    # explicit backup then restore the pre-edit snapshot from the existing entry
                    fresh_backup = await call("pvsyst_backup_component",
                                              {"component_type": "REV", "filename": FIXTURE_NAME})
                    self.assertEqual(fresh_backup["sha256"], reread["sha256"])
                    restored = await call("pvsyst_restore_component", {
                        "component_type": "REV", "filename": FIXTURE_NAME,
                        "backup_name": edited["backup_name"], "confirm": True})
                    self.assertTrue(restored["integrity_verified"])
                    self.assertEqual(fixture_path.read_bytes(), original)
                    self.assertEqual((await call("pvsyst_get_component", {
                        "component_type": "REV", "filename": FIXTURE_NAME}))["sha256"], sha0)

                    # clone (no Manufacturer/Model) with a text update, read back native fields
                    cloned = await call("pvsyst_clone_component", {
                        "component_type": "REV", "source_name": FIXTURE_NAME,
                        "new_name": "Cloned_Retailer.REV", "updates": {"Comment": "Cloned Seller"}})
                    self.assertEqual(cloned["format"], "text")
                    clone_info = await call("pvsyst_get_component",
                                            {"component_type": "REV", "filename": "Cloned_Retailer.REV"})
                    self.assertEqual(clone_info["fields"]["Comment"], ["Cloned Seller"])
                    self.assertEqual(clone_info["fields"]["Person"], ["Alice Example"])
                    self.assertEqual(clone_info["fields"]["Flags"], ["$80008022"])

                    # create from complete generated text
                    created = await call("pvsyst_create_component", {
                        "component_type": "REV", "filename": "Created_Retailer.REV",
                        "content": FIXTURE.replace("Contract Test Seller", "Created Seller")})
                    self.assertEqual(created["format"], "text")
                    created_info = await call("pvsyst_get_component", {
                        "component_type": "REV", "filename": "Created_Retailer.REV"})
                    self.assertEqual(created_info["fields"]["Comment"], ["Created Seller"])

                    # duplicate / no overwrite
                    await rejected("pvsyst_create_component", {
                        "component_type": "REV", "filename": FIXTURE_NAME, "content": FIXTURE})
                    await rejected("pvsyst_clone_component", {
                        "component_type": "REV", "source_name": FIXTURE_NAME,
                        "new_name": "Cloned_Retailer.REV", "updates": {}})

                    # path boundary
                    await rejected("pvsyst_get_component", {
                        "component_type": "REV", "filename": "../escape.REV"})

                    # inapplicable native features rejected, not silently ignored
                    await rejected("pvsyst_update_component", {
                        "component_type": "REV", "filename": FIXTURE_NAME, "updates": {},
                        "curve_updates": {"Converter/ProfilPIO": [[1.0, 1.0], [2.0, 2.0],
                                                                  [3.0, 3.0], [4.0, 4.0]]},
                        "expected_sha256": sha0})
                    await rejected("pvsyst_update_component", {
                        "component_type": "REV", "filename": FIXTURE_NAME, "updates": {},
                        "commercial_updates": {"Manufacturer": "X"},
                        "expected_sha256": sha0})

                    # original source is unchanged after every operation
                    self.assertEqual(fixture_path.read_bytes(), original)

            succeeded = True
        finally:
            if succeeded:
                shutil.rmtree(root)
            else:
                print("Retained REV sellers failure fixture: " + str(root), file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
