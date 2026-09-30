"""Opt-in read-only grid inventory and native loading in an independent copy.

PVSYST_GRID_CASES names JSON cases with project, variant, valid, totals and
optional start_date/end_date. Dates request a native run; otherwise only inspect.
PVSYST_WORKSPACE is read-only. PVSYST_EDITOR_LAB must be outside that workspace.
This verifies serialized counts and existing input loading, not capacity edits.
"""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import uuid

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvsyst_cli import parse_result_csv


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(root):
    return {str(p.relative_to(root)): fingerprint(p) for p in root.rglob("*") if p.is_file()}


async def main():
    source = Path(os.environ["PVSYST_WORKSPACE"]).resolve(strict=True)
    lab = Path(os.environ["PVSYST_EDITOR_LAB"]).resolve()
    if source == lab or source in lab.parents:
        raise ValueError("Lab must be outside source workspace")
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in source.rglob("*")):
        raise ValueError("Linked source entries are unsupported")
    before = manifest(source)
    cases = json.loads(Path(os.environ["PVSYST_GRID_CASES"]).read_text(encoding="utf-8-sig"))
    run = lab / ("mcp-grid-" + uuid.uuid4().hex[:12])
    root = run / source.name
    run.mkdir(parents=True, exist_ok=False)
    (run / "source_manifest.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    evidence = {"cli_sha256": fingerprint(Path(os.environ["PVSYST_CLI"])), "cases": []}
    print(f"Grid inventory/native verification: {run}", flush=True)
    try:
        shutil.copytree(source, root, ignore=lambda directory, names: (
            [name for name in names if name in ("Results", "UserHourly", "UserBatch")]
            if Path(directory) == source else []))
        for name in ("Results", "UserHourly", "UserBatch"):
            (root / name).mkdir(exist_ok=True)
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

                await call("pvsyst_build_sfi", filename="grid_inventory.sfi", variables="viGlobInc,viEArray,viE_Grid")
                for number, case in enumerate(cases):
                    record = {"request": case}
                    evidence["cases"].append(record)
                    target = {"project": case["project"], "variant": case["variant"]}
                    report = await call("pvsyst_validate_variant_structure", **target)
                    record["inspection"] = report
                    assert report["valid"] is case["valid"], report
                    assert report["grid_circuit"]["totals"] == case["totals"], report
                    if case.get("start_date"):
                        print(f"Simulating {target}...", flush=True)
                        result = await call("pvsyst_run_simulation", **target, sfi_name="grid_inventory.sfi",
                            csv_name=f"grid_inventory_{number}.csv", start_date=case["start_date"], end_date=case["end_date"])
                        csv = Path(result["csv"])
                        columns, rows = parse_result_csv(csv)
                        assert len(rows) == 48
                        metrics = {}
                        for name in ("EArray", "E_Grid"):
                            values = [row[columns.index(name)] for row in rows]
                            assert all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)
                            assert any(v > 0 for v in values)
                            metrics[name] = {"sum_raw_values": sum(values), "positive_rows": sum(v > 0 for v in values)}
                        record.update(csv=str(csv), csv_sha256=fingerprint(csv), rows=len(rows), native_loaded=True, metrics=metrics)
                    checked = await call("pvsyst_validate_variant_structure", **target)
                    assert checked["sha256"] == report["sha256"]
                    record["variant_unchanged"] = True
                    print(f"Verified {target}: valid={report['valid']}, totals={report['grid_circuit']['totals']}", flush=True)
    finally:
        evidence["source_unchanged"] = before == manifest(source)
        (run / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        assert evidence["source_unchanged"], "Source workspace changed"
    print("Native grid verification passed; original files unchanged", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
