"""Public preserve-source-hardware contract: wire shape and no-side-effect rejection.

No native executable is required: invalid requests are rejected at the public tool boundary and
a valid preserve request stops at the read-only version/licence preflight without writing.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from pydantic import ValidationError

from pvsyst_auto.api.schemas import SourcedHouseTaskRequest

REPO = Path(__file__).resolve().parents[2]
HOUSE = {"kind": "mesh", "meshes": [{"name": "m",
         "vertices": [[0, 0, 0], [1, 0, 0], [1, 1, 0]], "triangles": [[0, 1, 2]]}]}
LAYOUT = {"module_rows": 1, "module_columns": 1, "orientation": "portrait",
          "row_spacing_m": 0, "column_spacing_m": 0, "origin_m": [0, 0, 0], "roof_mesh": "m"}


def _base():
    return dict(new_project="House.PRJ", variant_name="v", house=HOUSE, layout=LAYOUT)


PRESERVE_INVALID = (
    dict(prepare_mode="preserve_source_hardware", source_project="Src.PRJ",
         source_variant="Src.VC0", design={"year": 1990, "target_dc_kw": 3.0}),
    dict(prepare_mode="preserve_source_hardware", source_project="Src.PRJ",
         source_variant="Src.VC0", design={"year": 1990, "tilt": 30}),
    dict(prepare_mode="preserve_source_hardware", source_project="Src.PRJ",
         design={"year": 1990}),
    dict(prepare_mode="preserve_source_hardware", source_project="Src.PRJ",
         source_variant="Src.VC0", design={}),
    dict(prepare_mode="preserve_source_hardware", source_project="Src.PRJ",
         source_variant="Src.VC0", design={"year": 1990}, goals={"target_dc_kw": 2.0}),
    dict(source_project="Src.PRJ", source_variant="Src.VC0", design={"year": 1990}),
)


class SourcedPreserveBoundary(unittest.TestCase):
    def test_public_request_shape(self):
        legacy = SourcedHouseTaskRequest(design={"new_project": "Src.PRJ", "year": 2024}, **_base())
        dumped = legacy.model_dump(exclude_none=True)
        for field in ("prepare_mode", "source_project", "source_variant"):
            self.assertNotIn(field, dumped)
        preserve = SourcedHouseTaskRequest(
            prepare_mode="preserve_source_hardware", source_project="Src.PRJ",
            source_variant="Src.VC0", design={"year": 1990}, **_base())
        dumped = preserve.model_dump(exclude_none=True)
        self.assertEqual(dumped["prepare_mode"], "preserve_source_hardware")
        self.assertEqual(dumped["source_project"], "Src.PRJ")
        self.assertEqual(dumped["source_variant"], "Src.VC0")
        for bad in PRESERVE_INVALID:
            with self.subTest(bad=sorted(bad)), self.assertRaises(ValidationError):
                SourcedHouseTaskRequest(**{**_base(), **bad})

    def test_public_tool_rejects_and_writes_nothing(self):
        asyncio.run(self._check())

    async def _check(self):
        with tempfile.TemporaryDirectory(prefix="pvsyst-preserve-boundary-") as directory:
            root = Path(directory) / "PVsyst8.1_Data"
            root.mkdir()
            fake = Path(directory) / "PVsystCLI.exe"
            fake.write_bytes(b"Not an executable")
            environment = dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(fake))
            environment.pop("PVSYST_BUILTIN_COMPONENTS", None)
            environment.pop("PVSYST_TOOL_GROUPS", None)
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=environment)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tool = next(t for t in (await session.list_tools()).tools
                                if t.name == "pvsyst_run_sourced_house_task")
                    self.assertEqual(set(tool.input_schema["properties"]),
                        {"spec", "dry_run", "confirm", "resume", "retry_simulation", "accept_source_review"})
                    for bad in PRESERVE_INVALID:
                        reply = await session.call_tool(tool.name, {"spec": {**_base(), **bad}})
                        self.assertTrue(reply.is_error, reply.content)
                    good = dict(prepare_mode="preserve_source_hardware", source_project="Src.PRJ",
                                source_variant="Src.VC0", design={"year": 1990})
                    reply = await session.call_tool(tool.name, {"spec": {**_base(), **good}, "dry_run": True})
                    self.assertFalse(reply.is_error, reply.content)
                    value = json.loads("\n".join(x.text for x in reply.content if hasattr(x, "text")))
                    self.assertEqual(value["status"], "blocked", value)
                    self.assertEqual(value["stage"], "native_preflight", value)
                    self.assertFalse(value["native_consumption_verified"])
                    self.assertIsNone(value["source_preparation"])
                    self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
