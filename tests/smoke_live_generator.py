"""Opt-in generator enable/disable comparison through MCP in independent copies.

Requires PVSYST_CLI, PVSYST_WORKSPACE, PVSYST_EDITOR_LAB,
PVSYST_EDITOR_PROJECT and PVSYST_EDITOR_GENERATOR. Optional VARIANT/START/END
use VC0 and 1990.01.01..02; choose a two-day standalone project period. Both
copies get the same generator configuration, with only enabled differing.
Uses two native simulations. Source files stay read-only; copies restore.
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


async def main():
    source = Path(os.environ["PVSYST_WORKSPACE"]).resolve(strict=True)
    lab = Path(os.environ["PVSYST_EDITOR_LAB"]).resolve()
    if lab == source or source in lab.parents:
        raise ValueError("Lab must be outside source workspace")
    paths = list(source.rglob("*"))
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in paths):
        raise ValueError("Source contains linked paths")
    before = {str(p.relative_to(source)): fingerprint(p) for p in paths if p.is_file()}
    run = lab / ("mcp-generator-" + uuid.uuid4().hex[:12])
    run.mkdir(parents=True, exist_ok=False)
    (run / "source_manifest.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    target = {"project": os.environ["PVSYST_EDITOR_PROJECT"], "variant": os.environ.get("PVSYST_EDITOR_VARIANT", "VC0")}
    evidence = {"target": target, "cli_sha256": fingerprint(Path(os.environ["PVSYST_CLI"])),
        "start_date": os.environ.get("PVSYST_EDITOR_START", "1990.01.01"),
        "end_date": os.environ.get("PVSYST_EDITOR_END", "1990.01.02"), "runs": {}}
    print(f"Generator enable/disable verification: {run}", flush=True)
    try:
        for enabled in (False, True):
            name = "enabled" if enabled else "disabled"
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
                        response = await session.call_tool(tool, args, read_timeout_seconds=1800)
                        if response.is_error:
                            raise RuntimeError(f"{tool}: {response.content}")
                        return json.loads(response.content[0].text)
                    info = await call("pvsyst_get_variant_parameters", **target)
                    assert info["generator"]["supported"]
                    settings = {"enabled": enabled, "filename": os.environ["PVSYST_EDITOR_GENERATOR"],
                        "operating_power_kw": float(os.environ.get("PVSYST_EDITOR_GENERATOR_KW", "40")),
                        "thresholds": {str(a["id"]): {"VBkUpEncl_syst": 0.9, "VBkUpDecl_syst": 0.95}
                                       for a in info["generator"]["subarrays"]}}
                    args = {**target, "expected_sha256": info["sha256"], "generator_updates": settings}
                    preview = await call("pvsyst_update_variant_parameters", **args, dry_run=True)
                    (run / (name + "_preview.json")).write_text(json.dumps(preview, indent=2), encoding="utf-8")
                    path = root / "Projects" / (Path(target["project"]).stem + "." + target["variant"])
                    assert fingerprint(path) == info["sha256"]
                    changed = await call("pvsyst_update_variant_parameters", **args)
                    record = {"before_sha256": info["sha256"], "candidate_sha256": changed["sha256"], "settings": settings}
                    evidence["runs"][name] = record
                    try:
                        assert changed["sha256"] == preview["sha256"]
                        await call("pvsyst_build_sfi", filename="generator.sfi",
                            variables="viGlobInc,viGlobEff,viEArray,viE_User,viE_BkUp,viFuelBU,viBkUp_ON")
                        print(f"Running {name}...", flush=True)
                        result = await call("pvsyst_run_simulation", **target, sfi_name="generator.sfi", csv_name="generator.csv",
                            start_date=evidence["start_date"], end_date=evidence["end_date"])
                        csv = Path(result["csv"])
                        columns, rows = parse_result_csv(csv)
                        assert len(rows) == 48
                        record.update(csv=str(csv), csv_sha256=fingerprint(csv), rows=len(rows), columns=columns, units=result_units(csv))
                        if enabled:
                            for field in ("E_BkUp", "FuelBU", "BkUp_ON"):
                                values = [r[columns.index(field)] for r in rows]
                                assert all(math.isfinite(v) and v >= 0 for v in values)
                                assert sum(values) > 0, f"No active generator {field}"
                            record["active_rows"] = sum(r[columns.index("BkUp_ON")] > 0 for r in rows)
                        else:
                            assert not {"E_BkUp", "FuelBU", "BkUp_ON"} & set(columns)
                        print(f"{name}: {len(rows)} rows, columns {columns}", flush=True)
                    finally:
                        if changed.get("backup_name"):
                            await call("pvsyst_restore_variant", **target, expected_sha256=changed["sha256"],
                                backup_name=changed["backup_name"], confirm=True)
                        record["restored_exactly"] = fingerprint(path) == info["sha256"]
                        assert record["restored_exactly"]
        a, ra = parse_result_csv(evidence["runs"]["enabled"]["csv"])
        b, rb = parse_result_csv(evidence["runs"]["disabled"]["csv"])
        for field in ("date", "GlobInc", "GlobEff"):
            assert [r[a.index(field)] for r in ra] == [r[b.index(field)] for r in rb]
        evidence["matched_timestamps_and_irradiation"] = True
    finally:
        after = {str(p.relative_to(source)): fingerprint(p) for p in source.rglob("*") if p.is_file()}
        evidence["source_unchanged"] = before == after
        (run / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        assert evidence["source_unchanged"]
    print(json.dumps(evidence, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
