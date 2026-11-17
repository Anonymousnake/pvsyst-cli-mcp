"""Existing opt-in multi-orientation execution, separated from offline authoring."""
import asyncio
import json
import math
import os
from pathlib import Path
import sys
import unittest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from offline.test_multi_orientation_native_boundary import (
    REPO, _payload, CREATE_TOOL, IMPORT_TOOL, READ_VARIANT_TOOL, NATIVE_NOT_LOADED,
)


class MultiOrientationNativeE2E(unittest.TestCase):
    def test_real_native_consumption_opt_in(self):
        """Consume the grouped scene with a REAL PVsyst install: import save -> ONE numeric batch -> TWO pure reads.

        Opt-in only: skipped unless PVSYST_MULTI_ORIENT_NATIVE_CONFIG points to a JSON file
        ``{"workspace","cli","project", "batch_params_name","batch_rvt_name", optional
        "scene_name"/"variant_name" or "create_args", optional "native_case_variant"}`` describing a
        genuine source project/variant. The guard rejects a fake/placeholder CLI so the real boundary is
        never reported green by an inert sentinel. The numeric run uses the ACTUAL saved VC id returned by
        the import (or a pre-saved ``native_case_variant`` consistent with the workspace) and a batch
        parameters/rvt pair, never the display name and never a guessed ``year``.
        """
        config_path = os.environ.get("PVSYST_MULTI_ORIENT_NATIVE_CONFIG")
        if not config_path:
            self.skipTest("real native NOTRUN: set PVSYST_MULTI_ORIENT_NATIVE_CONFIG (this stage is prep only)")
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        workspace, cli = Path(config["workspace"]), Path(config["cli"])
        if not cli.is_file() or cli.stat().st_size < 4096:
            self.fail("NoFakePAN/NoFakeCLI: real PVsyst CLI required, got %r" % (cli,))
        asyncio.run(self._native(workspace, cli, config))

    async def _native(self, workspace, cli, config):
        params = StdioServerParameters(command=sys.executable,
                                       args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO),
                                       env=dict(os.environ, PVSYST_WORKSPACE=str(workspace),
                                                PVSYST_CLI=str(cli)))
        raw_dir = os.environ.get("PVSYST_MULTI_ORIENT_RAW_DIR")
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(tool, args, tag):
                    reply = await session.call_tool(tool, args)
                    if raw_dir:                       # persist the FULL SDK reply BEFORE any handling
                        Path(raw_dir).mkdir(parents=True, exist_ok=True)
                        envelope = (reply.model_dump(mode="json") if hasattr(reply, "model_dump")
                                    else {"unmodeled": True})
                        Path(raw_dir, tag + ".sdk.json").write_text(
                            json.dumps(envelope, ensure_ascii=False, sort_keys=True, default=str),
                            encoding="utf-8")
                    return _payload(reply)            # raises on is_error; errors are never swallowed

                project = config.get("project") or (config.get("create_args") or {}).get("project")
                self.assertIsNotNone(project, "config needs 'project' (or create_args.project)")
                # Stage A (0 CLI): obtain the REAL saved native VC id, or reuse a pre-saved one.
                if config.get("native_case_variant"):
                    variant = config["native_case_variant"]
                else:
                    if config.get("scene_name"):
                        scene_name, variant_name = config["scene_name"], config.get("variant_name", "MultiNative")
                    else:
                        created = await call(CREATE_TOOL, dict(config["create_args"], dry_run=False), "create")
                        self.assertEqual(created["status"], "created", created)
                        scene_name, variant_name = config["create_args"]["output_name"], "MultiNative"
                    made = await call(IMPORT_TOOL, {"scene_name": scene_name, "variant_name": variant_name,
                                                    "method": "native", "shading_mode": "module_layout",
                                                    "dry_run": False, "confirm": True}, "import")
                    self.assertEqual(made["status"], "native_variant_created", made)
                    self.assertTrue(made["stored_module_layout_verified"], made)
                    for key in NATIVE_NOT_LOADED:      # import writes STORED only, no consumption
                        self.assertFalse(made[key], key)
                    project, variant = made["project"], made["variant"]     # actual returned VC id
                # ONE numeric BATCH run (real native consumption) on the SAVED variant id. Native
                # legitimately rewrites the DERIVED target (8.0/8.1.6 -> 8.1.7 SaveAs); that write is
                # the allowed business output and is judged by structure, never by whole-byte identity.
                run = await call("pvsyst_run_simulation", {
                    "project": project, "variant": variant,
                    "batch_params_name": config["batch_params_name"],
                    "batch_rvt_name": config.get("batch_rvt_name", "")}, "run")
                self.assertIsNone(run.get("pdf"), run)
                self.assertGreaterEqual(run.get("batch_runs") or 0, 1, run)
                for path in run.get("batch_hourly") or []:
                    self.assertTrue(Path(path).is_file(), "missing hourly CSV: %s" % path)
                # Optional Unicode text-integrity boundary: when the config declares an expected
                # non-ASCII display name, the ORIGINAL 8.1.7 save must keep the UTF-8 BOM and the
                # degree bytes, never the C3 82 C2 B0 mojibake. This is a file-byte assertion, not
                # an extra reader: the numeric run and two pure reads stay the same.
                if config.get("expect_unicode"):
                    saved_file = workspace / "Projects" / (Path(project).stem + "." + variant)
                    data = saved_file.read_bytes()
                    self.assertTrue(data.startswith(b"\xef\xbb\xbf"), "native save lost the UTF-8 BOM")
                    self.assertNotIn(b"\xc3\x82\xc2\xb0", data, "degree became Â° after native save")
                    self.assertIn(b"\xc2\xb0", data, "degree bytes missing after native save")
                    self.assertIn(config["display_name"].encode("utf-8"), data,
                                  "Unicode display name was re-encoded by the native save")
                # Pure re-read #1, AFTER consumption: the persisted source data must survive the native
                # save. The 8.1.7 after-file's stored layout classification may move off
                # stored_consistent (mlShd3DCalculated / non-PVCollada source), so assert the STATIC
                # SCENE NOTE (source orientation geometry, 90 modules / 6 strings capacity, active
                # scene, configured layout), never byte identity or a stored_consistent flag.
                saved = await call(READ_VARIANT_TOOL, {"project": project, "variant": variant},
                                   "readvariant")
                self.assertEqual([o["id"] for o in saved["orientations"]], [1, 2, 3])
                self.assertEqual([round(float(o["values"]["FieldTilt"]), 3) for o in saved["orientations"]],
                                 [25.0, 25.0, 25.0])
                self.assertEqual([round(float(o["values"]["FieldAzim"]), 3) for o in saved["orientations"]],
                                 [20.0, -70.0, 110.0])
                self.assertEqual([s["id"] for s in saved["subarrays"]], [1, 2, 3])
                self.assertEqual([int(s["values"]["NModSerie"]) for s in saved["subarrays"]], [15, 15, 15])
                self.assertEqual([int(s["values"]["NStringCh"]) for s in saved["subarrays"]], [2, 2, 2])
                self.assertEqual([int(s["values"]["NInvMPPT"]) for s in saved["subarrays"]], [1, 1, 1])
                self.assertEqual(sum(int(s["values"]["NModSerie"]) * int(s["values"]["NStringCh"])
                                     for s in saved["subarrays"]), 90)
                self.assertEqual(sum(int(s["values"]["NStringCh"]) for s in saved["subarrays"]), 6)
                scene = saved["shading_scene"]
                self.assertEqual(scene["mode"], "active", scene)
                self.assertTrue(scene["module_layout"]["configured"], scene)
                # Pure re-read #2: the produced batch summary (finite energy/PR). Same two pure readers
                # as the frozen flow; the variant read simply moved after the numeric run.
                summary_name = Path(run["batch_summary"]).name
                data = await call("pvsyst_read_batch_results", {"summary_name": summary_name}, "readbatch")
                self.assertTrue(data["scenarios"], data)
                for row in data["scenarios"]:
                    for column in ("E_Grid", "PR"):
                        value = row["values"][column]
                        self.assertIsInstance(value, (int, float))
                        self.assertTrue(math.isfinite(value), "%s not finite" % column)


if __name__ == "__main__":
    unittest.main()
