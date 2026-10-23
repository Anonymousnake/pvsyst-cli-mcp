"""Public saved-input review/difference/no-write contract with explicit native-export fixture."""
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


class ProjectInputReviewBoundary(unittest.TestCase):
    def test_public_saved_input_review(self):
        fixture = os.environ.get("PVSYST_PROJECT_REVIEW_FIXTURE")
        if not fixture:
            self.skipTest("Explicit prepared native-export fixture required; no native-runtime claim")
        asyncio.run(self.check(Path(fixture)))

    async def check(self, fixture):
        workspace = fixture / "PVsyst8.1_Data"
        name = "RESIDENTIAL_INPUT_prepare_2024.json"
        raw = (workspace / "Results" / name).read_bytes()
        baseline = json.loads(raw)
        source, variant = baseline["working_project"], baseline["working_variant"]
        directory = Path(tempfile.mkdtemp(prefix="pvsyst-project-review-"))
        try:
            root = directory / "PVsyst8.1_Data"
            for folder in ("Projects", "Results", "Sites", "Meteo"):
                (root / folder).mkdir(parents=True)
            files = [("Projects", source), ("Projects", Path(source).stem + "." + variant),
                     ("Sites", baseline["request"]["site_name"]), ("Meteo", baseline["request"]["met_name"])]
            for folder, filename in files:
                self.assertEqual(Path(filename).name, filename)
                shutil.copy2(workspace / folder / filename, root / folder / filename)
            (root / "Results" / name).write_bytes(raw)
            fake = directory / "PVsystCLI.exe"
            fake.write_bytes(b"Not an executable")
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(fake)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tool = next(t for t in (await session.list_tools()).tools if t.name == "pvsyst_review_project_inputs")
                    self.assertEqual(set(tool.input_schema["properties"]), {"project", "variant", "preparation_manifest_name"})
                    async def call(name, args):
                        reply = await session.call_tool(name, args)
                        self.assertFalse(reply.is_error, reply.content)
                        return json.loads("\n".join(x.text for x in reply.content if hasattr(x, "text")))
                    args = dict(project=source, variant=variant, preparation_manifest_name=name)
                    original = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
                    members = sorted(root.rglob("*"))
                    value = await call(tool.name, args)
                    self.assertEqual(original, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})
                    self.assertEqual(members, sorted(root.rglob("*")))
                    self.assertEqual(value["differences"], [], value)
                    self.assertTrue(value["readonly"])
                    self.assertFalse(value["native_consumption_verified"])
                    self.assertFalse(value["runtime_state_observed"])
                    self.assertFalse(value["repairs_applied"])
                    self.assertTrue(any(x["status"] == "not_directly_comparable" for x in value["declared_not_native"]))
                    self.assertFalse(any(x["field"] in ("DesignTempMin", "TempOper", "TempOperMin", "TempOperMax") for x in value["comparisons"]))
                    await call("pvsyst_clone_project", dict(source_project=source, new_project="ReviewTarget.PRJ"))
                    state = await call("pvsyst_get_variant_parameters", dict(project="ReviewTarget.PRJ", variant=variant))
                    changed = float(baseline["request"]["tilt"]) + 1
                    await call("pvsyst_update_variant_parameters", dict(project="ReviewTarget.PRJ", variant=variant,
                        expected_sha256=state["sha256"], subarray_id=state["subarrays"][0]["id"],
                        orientation_id=state["orientations"][0]["id"], orientation_updates={"FieldTilt": changed}))
                    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
                    value = await call(tool.name, dict(args, project="ReviewTarget.PRJ"))
                    self.assertEqual(value["status"], "needs_review")
                    self.assertEqual([row["field"] for row in value["differences"]], ["tilt"])
                    self.assertEqual(float(value["differences"][0]["actual_saved"]), changed)
                    self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})
                    met = root / "Meteo" / baseline["request"]["met_name"]
                    met_bytes = met.read_bytes()
                    met.unlink()
                    unresolved = await call(tool.name, args)
                    self.assertEqual(unresolved["status"], "needs_review")
                    self.assertTrue(unresolved["source_reference_warnings"])
                    self.assertEqual(unresolved["differences"], [])
                    met.write_bytes(met_bytes)
                    native_variant = root / "Projects" / (Path(source).stem + "." + variant)
                    native_bytes = native_variant.read_bytes()
                    for field in (b"UsesAzimAver", b"UsesSlopeAver"):
                        if field + b"=False" in native_bytes:
                            payload = native_bytes.replace(field + b"=False", field + b"=True", 1)
                        else:
                            needle = b"FieldType=FixedPlane"
                            self.assertEqual(native_bytes.count(needle), 1)
                            payload = native_bytes.replace(needle, needle + b"\n            " + field + b"=True", 1)
                        native_variant.write_bytes(payload)
                        blocked = await call(tool.name, args)
                        self.assertEqual(blocked["status"], "blocked")
                        self.assertEqual(native_variant.read_bytes(), payload)
                    native_variant.write_bytes(native_bytes)
                    bad_name = dict(baseline, request=dict(baseline["request"], pan_name="CON.PAN"))
                    payload = json.dumps(bad_name).encode("utf-8")
                    for payload in (payload, b"[]", b"null", b'{"operation":"unknown"}'):
                        (root / "Results" / name).write_bytes(payload)
                        blocked = await call(tool.name, args)
                        self.assertEqual(blocked["status"], "blocked")
                        self.assertEqual((root / "Results" / name).read_bytes(), payload)
                    (root / "Results" / name).write_bytes(raw)
                    blocked = await call(tool.name, dict(args, preparation_manifest_name="../outside.json"))
                    self.assertEqual(blocked["status"], "blocked")
                    reply = await session.call_tool(tool.name, dict(args, unexpected=True))
                    self.assertTrue(reply.is_error)
                    self.assertEqual((root / "Results" / name).read_bytes(), raw)
        except BaseException:
            print(f"Project review failure artifacts retained: {directory}", file=sys.stderr)
            raise
        else:
            shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()
