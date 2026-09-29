"""Opt-in OND curve consumption test through real MCP stdio and PVsystCLI.

Requires PVSYST_CLI, PVSYST_WORKSPACE, PVSYST_EDITOR_PROJECT,
PVSYST_EDITOR_INVERTER and PVSYST_EDITOR_LAB. Optional variant/start/end
variables match smoke_live_component_editor.py (defaults: VC0, two days in 2022).
Consumes two simulations in independent copies, never edits source inputs.
Keeps previews, CSVs, source hashes and evidence.json in a new LAB directory.
This checks native consumption of points, not physical model certification.
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
from pvsyst_cli import parse_result_csv, summarize
from smoke_live_component_editor import fingerprint


def verify_curve_outputs(evidence):
    a, ra = parse_result_csv(evidence["runs"]["file_original"]["csv"])
    b, rb = parse_result_csv(evidence["runs"]["file_090"]["csv"])
    if len(ra) != 48 or len(rb) != 48:
        raise RuntimeError("Expected 48 hourly rows in each two-day run")
    matched = []
    # Compare equal DC input and equal exported non-operating losses. Do not
    # infer point semantics from total energy: limiting states can change.
    controls = ("EArray", "IL_Pmin", "IL_Pmax", "IL_Vmin", "IL_Vmax", "IL_Imax")
    for x, y in zip(ra, rb):
        x, y = dict(zip(a, x)), dict(zip(b, y))
        assert all(x[k] == y[k] for k in ("date", "GlobInc", "GlobEff"))
        if x["E_Grid"] > 0.01 and all(x[k] == y[k] for k in controls):
            error = abs(y["E_Grid"] - 0.9 * x["E_Grid"])
            matched.append({"date": str(x["date"]), "dc_input": x["EArray"],
                            "original_ac": x["E_Grid"], "edited_ac": y["E_Grid"],
                            "absolute_error": error})
    evidence["matched_input_rows"] = matched
    if len(matched) < 4 or any(not math.isfinite(row["absolute_error"])
                             or row["absolute_error"] > 0.0001 for row in matched):
        raise RuntimeError("Need >=4 matched-input rows with 90% AC output within CSV rounding")
    evidence["native_curve_consumption_verified"] = True
    evidence["physical_model_certified"] = False


async def main():
    source = Path(os.environ["PVSYST_WORKSPACE"]).resolve(strict=True)
    lab = Path(os.environ["PVSYST_EDITOR_LAB"]).resolve()
    if lab == source or source in lab.parents:
        raise ValueError("Lab must be outside the source workspace")
    paths = list(source.rglob("*"))
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in paths):
        raise ValueError("Source workspace contains linked paths")
    before = {str(p.relative_to(source)): fingerprint(p) for p in paths if p.is_file()}
    run = lab / ("mcp-curves-" + uuid.uuid4().hex[:12])
    run.mkdir(parents=True, exist_ok=False)
    (run / "source_manifest.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    project = os.environ["PVSYST_EDITOR_PROJECT"]
    inverter = os.environ["PVSYST_EDITOR_INVERTER"]
    variant = os.environ.get("PVSYST_EDITOR_VARIANT", "VC0")
    evidence = {"cli_sha256": fingerprint(Path(os.environ["PVSYST_CLI"])),
                "project": project, "variant": variant, "inverter": inverter,
                "start_date": os.environ.get("PVSYST_EDITOR_START", "2022.01.01"),
                "end_date": os.environ.get("PVSYST_EDITOR_END", "2022.01.02"), "runs": {}}
    print(f"Isolated MCP curve verification: {run}", flush=True)
    try:
        for name, multiplier in (("file_original", 1), ("file_090", 0.9)):
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

                    references = await call("pvsyst_project_components", project=project, variant=variant)
                    if not any(r["type"] == "OND" and r["name"] == inverter for r in references["references"]):
                        raise ValueError("Project must reference the selected inverter")
                    target = {"component_type": "OND", "filename": inverter}
                    info = await call("pvsyst_get_component", **target)
                    curve = next(c for c in info["curves"]["items"] if c["path"] == "Converter/ProfilPIO")
                    args = {**target, "updates": {}, "expected_sha256": info["sha256"],
                            "use_file_curve": True,
                            "curve_updates": {curve["path"]: [[x, y * multiplier] for x, y in curve["points"]]}}
                    preview = await call("pvsyst_update_component", **args, dry_run=True)
                    (run / (name + "_preview.json")).write_text(json.dumps(preview, indent=2), encoding="utf-8")
                    path = root / "ComposPV" / "Inverters" / inverter
                    assert fingerprint(path) == info["sha256"]
                    edited = await call("pvsyst_update_component", **args)
                    try:
                        assert edited["sha256"] == preview["sha256"]
                        assert edited["curve_control_after"]["source"] == "file"
                        sfi_name = "curve_" + run.name + ".sfi"
                        await call("pvsyst_build_sfi", filename=sfi_name, variables="all_common")
                        print(f"Running {name}...", flush=True)
                        output = await call("pvsyst_run_simulation", project=project, variant=variant,
                            sfi_name=sfi_name, csv_name="curve_check.csv",
                            start_date=evidence["start_date"], end_date=evidence["end_date"])
                        csv = Path(output["csv"])
                        summary = summarize(csv)
                        assert summary["complete"] and summary["energy_kwh"] is not None
                        evidence["runs"][name] = {"csv": str(csv), "csv_sha256": fingerprint(csv),
                            "energy_kwh": summary["energy_kwh"], "rows": summary["rows"],
                            "component_before_sha256": info["sha256"], "component_sha256": edited["sha256"]}
                        print(f"{name}: {summary['rows']} rows, {summary['energy_kwh']:.6f} kWh", flush=True)
                    finally:
                        if edited.get("backup_name"):
                            await call("pvsyst_restore_component", **target,
                                       backup_name=edited["backup_name"], confirm=True)
                        assert fingerprint(path) == info["sha256"]
                        evidence.setdefault("restored_exactly", {})[name] = True
        verify_curve_outputs(evidence)
    finally:
        after = {str(p.relative_to(source)): fingerprint(p) for p in source.rglob("*") if p.is_file()}
        evidence["source_unchanged"] = before == after
        (run / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        assert evidence["source_unchanged"], "Source workspace changed during verification"
    print(json.dumps(evidence, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
