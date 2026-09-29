"""Opt-in BTR capacity verification through real MCP and PVsystCLI.

Requires PVSYST_CLI, PVSYST_WORKSPACE, PVSYST_EDITOR_PROJECT,
PVSYST_EDITOR_BATTERY, PVSYST_EDITOR_LAB and PVSYST_BATTERY_POINTS.
POINTS is a JSON file containing all active [duration_hours, relative_capacity]
pairs suitable for the selected battery. Supply an independently chosen valid
curve; this script does not fit a model. Both it and its Y*0.9 curve must pass.
Optional PVSYST_EDITOR_VARIANT/START/END default to VC0 and two days in 1990.
Uses two simulations in independent copies; preserves source files and local
evidence. Tests curve consumption, not physical accuracy of the battery model.
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
from pvsyst_battery_editor import capacity_ratio
from smoke_live_component_editor import fingerprint


def verify_outputs(evidence):
    for run in evidence["runs"].values():
        assert result_units(run["csv"])["CapaEff"] == "Ah"
    a, ra = parse_result_csv(evidence["runs"]["file_100"]["csv"])
    b, rb = parse_result_csv(evidence["runs"]["file_090"]["csv"])
    assert len(ra) == len(rb) == 48, "Expected 48 rows in each run"
    capacities = [[], []]
    for x, y in zip(ra, rb):
        x, y = dict(zip(a, x)), dict(zip(b, y))
        assert all(x[k] == y[k] for k in ("date", "GlobInc", "GlobEff"))
        for i, row in enumerate((x, y)):
            value = row["CapaEff"]
            assert value is not None and math.isfinite(value) and value > 0
            capacities[i].append(value)
    means = [sum(values) / len(values) for values in capacities]
    ratio = means[1] / means[0]
    # Coupled battery state may slightly change capacity in addition to the
    # imposed ordinate scale. This is a consumption check, not an exact fit.
    assert 0.88 <= ratio <= 0.92, f"Expected roughly 90% capacity; observed {ratio}"
    evidence.update(mean_capacity_original_ah=means[0], mean_capacity_edited_ah=means[1],
                    capacity_mean_ratio=ratio, native_curve_consumption_verified=True,
                    physical_model_certified=False)


async def main():
    source = Path(os.environ["PVSYST_WORKSPACE"]).resolve(strict=True)
    lab = Path(os.environ["PVSYST_EDITOR_LAB"]).resolve()
    if lab == source or source in lab.parents:
        raise ValueError("Lab must be outside the source workspace")
    paths = list(source.rglob("*"))
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in paths):
        raise ValueError("Source workspace contains linked paths")
    points = json.loads(Path(os.environ["PVSYST_BATTERY_POINTS"]).read_text(encoding="utf-8-sig"))
    capacity_ratio(points)
    scaled = [[x, y * 0.9] for x, y in points]
    capacity_ratio(scaled)
    before = {str(p.relative_to(source)): fingerprint(p) for p in paths if p.is_file()}
    run = lab / ("mcp-battery-" + uuid.uuid4().hex[:12])
    run.mkdir(parents=True, exist_ok=False)
    (run / "source_manifest.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    project = os.environ["PVSYST_EDITOR_PROJECT"]
    battery = os.environ["PVSYST_EDITOR_BATTERY"]
    variant = os.environ.get("PVSYST_EDITOR_VARIANT", "VC0")
    evidence = {"cli_sha256": fingerprint(Path(os.environ["PVSYST_CLI"])),
                "project": project, "variant": variant, "battery": battery,
                "start_date": os.environ.get("PVSYST_EDITOR_START", "1990.01.01"),
                "end_date": os.environ.get("PVSYST_EDITOR_END", "1990.01.02"), "runs": {}}
    print(f"Isolated MCP battery verification: {run}", flush=True)
    try:
        for name, supplied in (("file_100", points), ("file_090", scaled)):
            root = run / name / source.name
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

                    refs = await call("pvsyst_project_components", project=project, variant=variant)
                    if not any(r["type"] == "BTR" and r["name"] == battery for r in refs["references"]):
                        raise ValueError("Project must reference the selected battery")
                    target = {"component_type": "BTR", "filename": battery}
                    info = await call("pvsyst_get_component", **target)
                    curve_path = info["curves"]["control"]["path"]
                    args = {**target, "updates": {}, "expected_sha256": info["sha256"],
                            "use_file_curve": True, "curve_updates": {curve_path: supplied}}
                    preview = await call("pvsyst_update_component", **args, dry_run=True)
                    (run / (name + "_preview.json")).write_text(json.dumps(preview, indent=2), encoding="utf-8")
                    path = root / "ComposPV" / "Batteries" / battery
                    assert fingerprint(path) == info["sha256"]
                    edited = await call("pvsyst_update_component", **args)
                    try:
                        assert edited["sha256"] == preview["sha256"]
                        assert edited["curve_control_after"]["path"] == "CapaCourant"
                        sfi_name = "battery_" + run.name + ".sfi"
                        await call("pvsyst_build_sfi", filename=sfi_name,
                            variables="viGlobInc,viGlobEff,viEArray,viCapaEff,viSOC_End,viU_Batt,viNBatCyc")
                        print(f"Running {name}...", flush=True)
                        output = await call("pvsyst_run_simulation", project=project, variant=variant,
                            sfi_name=sfi_name, csv_name="battery_check.csv",
                            start_date=evidence["start_date"], end_date=evidence["end_date"])
                        csv = Path(output["csv"])
                        columns, rows = parse_result_csv(csv)
                        assert "CapaEff" in columns and len(rows) == 48
                        evidence["runs"][name] = {"csv": str(csv), "csv_sha256": fingerprint(csv),
                            "rows": len(rows), "component_before_sha256": info["sha256"],
                            "component_sha256": edited["sha256"], "ratio_100h": capacity_ratio(supplied)}
                        print(f"{name}: {len(rows)} rows", flush=True)
                    finally:
                        if edited.get("backup_name"):
                            await call("pvsyst_restore_component", **target,
                                       backup_name=edited["backup_name"], confirm=True)
                        assert fingerprint(path) == info["sha256"]
                        evidence.setdefault("restored_exactly", {})[name] = True
        verify_outputs(evidence)
    finally:
        after = {str(p.relative_to(source)): fingerprint(p) for p in source.rglob("*") if p.is_file()}
        evidence["source_unchanged"] = before == after
        (run / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        assert evidence["source_unchanged"], "Source workspace changed during verification"
    print(json.dumps(evidence, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
