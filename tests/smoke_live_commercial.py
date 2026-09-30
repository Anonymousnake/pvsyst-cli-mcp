"""Opt-in commercial form edits consumed by native referencing simulations.

PVSYST_CLI, PVSYST_WORKSPACE, PVSYST_EDITOR_LAB and PVSYST_COMMERCIAL_CASES
are required. CASES names a local JSON file containing objects with project,
variant, start_date/end_date (two days), and components {type: filename}.
Optional scalar_updates maps component types to scalar field dictionaries.
Each case runs once in an independent copy through real MCP. Source data is
read-only; all edited copies restore in finally. This verifies native loading,
not GUI round-trip fidelity, seller pricing or physical model equivalence.
"""
import asyncio
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvsyst_cli import parse_result_csv
from pvsyst_components import KINDS
from smoke_live_component_editor import fingerprint


async def main():
    source = Path(os.environ["PVSYST_WORKSPACE"]).resolve(strict=True)
    lab = Path(os.environ["PVSYST_EDITOR_LAB"]).resolve()
    if lab == source or source in lab.parents:
        raise ValueError("Lab must be outside the source workspace")
    paths = list(source.rglob("*"))
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in paths):
        raise ValueError("Source workspace contains linked paths")
    cases = json.loads(Path(os.environ["PVSYST_COMMERCIAL_CASES"]).read_text(encoding="utf-8-sig"))
    before = {str(p.relative_to(source)): fingerprint(p) for p in paths if p.is_file()}
    run = lab / ("mcp-commercial-" + uuid.uuid4().hex[:12])
    run.mkdir(parents=True, exist_ok=False)
    (run / "source_manifest.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    evidence = {"cli_sha256": fingerprint(Path(os.environ["PVSYST_CLI"])), "cases": []}
    print(f"Native commercial verification: {run}", flush=True)
    try:
        for number, case in enumerate(cases):
            root = run / str(number) / source.name
            shutil.copytree(source, root, ignore=lambda directory, names: (
                [n for n in names if n in ("Results", "UserHourly", "UserBatch")]
                if Path(directory) == source else []))
            for folder in ("Results", "UserHourly", "UserBatch"):
                (root / folder).mkdir(exist_ok=True)
            record = {"request": case, "components": {}, "restored": {}}
            evidence["cases"].append(record)
            params = StdioServerParameters(command=sys.executable,
                args=[str(Path(__file__).resolve().parents[1] / "pvsyst_mcp_server.py")],
                env={**os.environ, "PVSYST_WORKSPACE": str(root)})
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()

                    async def call(tool, **args):
                        response = await session.call_tool(tool, args, read_timeout_seconds=1800)
                        if response.is_error:
                            raise RuntimeError(f"{tool}: {response.content}")
                        return json.loads(response.content[0].text)

                    edits = []
                    try:
                        refs = await call("pvsyst_project_components", project=case["project"], variant=case["variant"])
                        record["references"] = refs
                        for kind, filename in case["components"].items():
                            assert any(r["type"] == kind and r["name"] == filename for r in refs["references"])
                            target = {"component_type": kind, "filename": filename}
                            info = await call("pvsyst_get_component", **target)
                            form = info["commercial"]
                            assert form["editable"] and not form["remarks_truncated"]
                            changes = {"Manufacturer": "MCP Verification", "Model": "Commercial " + kind,
                                "DataSource": "Local form verification", "YearBeg": "2026",
                                "Weight": "30", "NPieces": "2", "PriceDate": "30/09/26 01:00"}
                            for field in ("Width", "Height", "Depth"):
                                changes[field] = str(float(form["fields"][field]["value"]) * 1.01)
                            args = {**target, "updates": case.get("scalar_updates", {}).get(kind, {}), "expected_sha256": info["sha256"],
                                    "commercial_updates": changes,
                                    "remarks": ["Local smoke test", "Edited through MCP", "Reversible form edit"]}
                            preview = await call("pvsyst_update_component", **args, dry_run=True)
                            path = root / "ComposPV" / KINDS[kind][0] / filename
                            assert fingerprint(path) == info["sha256"]
                            changed = await call("pvsyst_update_component", **args)
                            edits.append((target, path, info["sha256"], changed["backup_name"]))
                            assert changed["sha256"] == preview["sha256"]
                            record["components"][kind] = {"before_sha256": info["sha256"],
                                                        "candidate_sha256": changed["sha256"]}
                            (run / f"{number}_{kind}_preview.json").write_text(json.dumps(preview, indent=2), encoding="utf-8")
                        await call("pvsyst_build_sfi", filename="commercial.sfi", variables="viGlobInc,viGlobEff,viEArray")
                        print(f"Simulating {case['project']} / {case['variant']}...", flush=True)
                        result = await call("pvsyst_run_simulation", project=case["project"], variant=case["variant"],
                            sfi_name="commercial.sfi", csv_name="commercial.csv",
                            start_date=case["start_date"], end_date=case["end_date"])
                        csv = Path(result["csv"])
                        columns, rows = parse_result_csv(csv)
                        assert len(rows) == 48 and "EArray" in columns
                        record.update(csv=str(csv), csv_sha256=fingerprint(csv), rows=len(rows), native_load_passed=True)
                        print(f"Native load passed: {', '.join(case['components'])}, {len(rows)} rows", flush=True)
                    finally:
                        for target, path, original, backup in reversed(edits):
                            await call("pvsyst_restore_component", **target, backup_name=backup, confirm=True)
                            assert fingerprint(path) == original
                            record["restored"][target["component_type"]] = True
    finally:
        after = {str(p.relative_to(source)): fingerprint(p) for p in source.rglob("*") if p.is_file()}
        evidence["source_unchanged"] = before == after
        (run / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        assert evidence["source_unchanged"], "Source workspace changed"
    print(json.dumps(evidence, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
