"""Opt-in point-table grow/shrink verification via MCP and PVsystCLI.

Requires PVSYST_CLI, PVSYST_WORKSPACE, PVSYST_EDITOR_LAB and
PVSYST_CURVE_TABLE_CASES (external JSON array). Each case supplies kind
(OND/BTR), filename, project, variant, start_date, end_date, output_column,
and points={baseline:[...], grow:[...], shrink:[...]}. Tables must be valid
for the selected component; each changed curve must measurably affect output.
Uses three independent workspace copies/simulations per case. Source files
are read only; evidence and exact component restorations are retained in LAB.
This checks consumption, not physical accuracy or GUI save/reopen behavior.
"""
import asyncio
import json
import math
import os
import shutil
import sys
import uuid
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvsyst_cli import parse_result_csv, result_units
from smoke_live_component_editor import fingerprint


def verify_outputs(case, runs):
    column = case["output_column"]
    headers, rows = parse_result_csv(runs["baseline"]["csv"])
    base = [dict(zip(headers, row)) for row in rows]
    units = result_units(runs["baseline"]["csv"])
    assert len(base) == 48, "Expected two days of hourly output"
    for name in ("grow", "shrink"):
        headers, rows = parse_result_csv(runs[name]["csv"])
        edited = [dict(zip(headers, row)) for row in rows]
        assert len(edited) == len(base)
        assert result_units(runs[name]["csv"])[column] == units[column]
        changes = []
        for a, b in zip(base, edited):
            assert all(a[key] == b[key] for key in ("date", "GlobInc", "GlobEff"))
            assert all(isinstance(row[column], (float, int)) and math.isfinite(row[column])
                       for row in (a, b))
            changes.append(b[column] - a[column])
        assert any(abs(value) > 1e-6 for value in changes), (name, "No measurable output change")
        runs[name]["comparison"] = {"column": column, "unit": units[column],
            "changed_rows": sum(abs(value) > 1e-6 for value in changes),
            "max_absolute_delta": max(map(abs, changes)), "mean_delta": sum(changes) / len(changes)}


async def main():
    source = Path(os.environ["PVSYST_WORKSPACE"]).resolve(strict=True)
    lab = Path(os.environ["PVSYST_EDITOR_LAB"]).resolve()
    if lab == source or source in lab.parents:
        raise ValueError("Lab must be outside source workspace")
    paths = list(source.rglob("*"))
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in paths):
        raise ValueError("Linked source paths are unsupported")
    cases = json.loads(Path(os.environ["PVSYST_CURVE_TABLE_CASES"]).read_text(encoding="utf-8-sig"))
    if not cases:
        raise ValueError("Supply at least one case")
    for case in cases:
        if case["kind"] not in ("OND", "BTR"):
            raise ValueError("Only OND/BTR cases are supported")
        sizes = {name: len(points) for name, points in case["points"].items()}
        assert sizes["shrink"] < sizes["baseline"] < sizes["grow"]
    before = {str(p.relative_to(source)): fingerprint(p) for p in paths if p.is_file()}
    run = lab / ("mcp-curve-tables-" + uuid.uuid4().hex[:12])
    run.mkdir(parents=True, exist_ok=False)
    (run / "source_manifest.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    evidence = {"cli_sha256": fingerprint(Path(os.environ["PVSYST_CLI"])), "cases": [],
                "physical_model_certified": False, "gui_roundtrip_verified": False}
    print(f"Curve table verification: {run}", flush=True)
    try:
        for index, case in enumerate(cases):
            item = {"configuration": case, "runs": {}}
            evidence["cases"].append(item)
            for name in ("baseline", "grow", "shrink"):
                points = case["points"][name]
                root = run / f"{index}_{case['kind']}_{name}" / "PVsyst8.1_Data"
                shutil.copytree(source, root, ignore=lambda directory, names: (
                    [n for n in names if n in ("Results", "UserHourly", "UserBatch")]
                    if Path(directory) == source else []))
                for folder in ("Results", "UserHourly", "UserBatch"):
                    (root / folder).mkdir(exist_ok=True)
                params = StdioServerParameters(command=sys.executable,
                    args=[str(Path(__file__).resolve().parents[1] / "pvsyst_mcp_server.py")],
                    env={**os.environ, "PVSYST_WORKSPACE": str(root)})
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()

                        async def call(tool, **args):
                            result = await session.call_tool(tool, args, read_timeout_seconds=1800)
                            if result.is_error:
                                raise RuntimeError(f"{tool}: {result.content}")
                            return json.loads(result.content[0].text)

                        refs = await call("pvsyst_project_components", project=case["project"], variant=case["variant"])
                        assert any(r["type"] == case["kind"] and r["name"] == case["filename"] for r in refs["references"])
                        target = {"component_type": case["kind"], "filename": case["filename"]}
                        original = await call("pvsyst_get_component", **target)
                        curve = "Converter/ProfilPIO" if case["kind"] == "OND" else original["curves"]["control"]["path"]
                        args = {**target, "updates": {}, "curve_updates": {curve: points},
                                "expected_sha256": original["sha256"], "use_file_curve": True}
                        preview = await call("pvsyst_update_component", **args, dry_run=True)
                        (root.parent / "preview.json").write_text(json.dumps(preview, indent=2), encoding="utf-8")
                        assert (await call("pvsyst_get_component", **target))["sha256"] == original["sha256"]
                        edit = await call("pvsyst_update_component", **args)
                        try:
                            assert edit["sha256"] == preview["sha256"]
                            curve = "CapaCourant" if case["kind"] == "BTR" else curve
                            inspected = await call("pvsyst_get_component", **target)
                            updated = next(c for c in inspected["curves"]["items"] if c["path"] == curve)
                            assert updated["points"] == points and updated["point_count"] == len(points)
                            previous = next(c for c in original["curves"]["items"] if c["path"] == next(iter(args["curve_updates"])))
                            if len(points) != previous["point_count"]:
                                assert updated["allocated_point_count"] == len(points)
                            noop = await call("pvsyst_update_component", **{**args,
                                "expected_sha256": edit["sha256"], "curve_updates": {curve: points}})
                            assert not noop["changed"] and not noop.get("backup_name")
                            sfi = "curve_table_check.sfi"
                            await call("pvsyst_build_sfi", filename=sfi,
                                       variables=f"viGlobInc,viGlobEff,viEArray,vi{case['output_column']}")
                            print(f"Running {case['kind']} {name}, {len(points)} points...", flush=True)
                            output = await call("pvsyst_run_simulation", project=case["project"], variant=case["variant"],
                                sfi_name=sfi, csv_name="curve_table_check.csv", start_date=case["start_date"], end_date=case["end_date"])
                            csv = Path(output["csv"])
                            item["runs"][name] = {"csv": str(csv), "csv_sha256": fingerprint(csv),
                                "component_before_sha256": original["sha256"], "component_sha256": edit["sha256"],
                                "counts": edit["curve_counts_after"], "rows": len(parse_result_csv(csv)[1])}
                            print(f"{case['kind']} {name}: {item['runs'][name]['rows']} rows", flush=True)
                        finally:
                            if edit.get("backup_name"):
                                await call("pvsyst_restore_component", **target, backup_name=edit["backup_name"], confirm=True)
                            assert (await call("pvsyst_get_component", **target))["sha256"] == original["sha256"]
                            item.setdefault("restored_exactly", {})[name] = True
            verify_outputs(case, item["runs"])
            item["native_curve_consumption_verified"] = True
    finally:
        after = {str(p.relative_to(source)): fingerprint(p) for p in source.rglob("*") if p.is_file()}
        evidence["source_unchanged"] = before == after
        (run / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        assert evidence["source_unchanged"], "Source changed during verification"
    print(json.dumps(evidence, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
