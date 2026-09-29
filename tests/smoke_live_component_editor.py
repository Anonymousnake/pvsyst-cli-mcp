"""Opt-in two-run guarded scalar verification in independent workspace copies.

Requires PVSYST_CLI, PVSYST_WORKSPACE, PVSYST_EDITOR_PROJECT,
PVSYST_EDITOR_INVERTER and PVSYST_EDITOR_LAB. Source files are only read.
Uses VC0 by default and two days in 2022; override PVSYST_EDITOR_VARIANT,
PVSYST_EDITOR_START and PVSYST_EDITOR_END for the selected project.
Consumes two simulations. Keeps copies, CSVs and evidence.json under LAB.
"""
import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvsyst_cli import PVsystCLI, build_sfi, parse_result_csv, summarize
from pvsyst_components import ComponentStore


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_outputs(evidence):
    baseline = evidence["runs"]["baseline"]["energy_kwh"]
    changed = evidence["runs"]["edited"]["energy_kwh"]
    # This checks consumption of an edited scalar, not the validity or monotonic
    # performance of a resized system whose other parameters were not refitted.
    if baseline <= 0 or changed <= 0 or abs(changed - baseline) < 0.001:
        raise RuntimeError("Inverter rating edit did not measurably change grid energy")
    a = parse_result_csv(evidence["runs"]["baseline"]["csv"])
    b = parse_result_csv(evidence["runs"]["edited"]["csv"])
    for field in ("GlobInc", "GlobEff"):
        assert [r[a[0].index(field)] for r in a[1]] == [r[b[0].index(field)] for r in b[1]], field
    evidence["energy_change_percent"] = (changed / baseline - 1) * 100
    evidence["physical_model_certified"] = False


def main():
    source = Path(os.environ["PVSYST_WORKSPACE"]).resolve(strict=True)
    lab = Path(os.environ["PVSYST_EDITOR_LAB"]).resolve()
    if lab == source or source in lab.parents:
        raise ValueError("Lab must be outside the source workspace")
    run = lab / uuid.uuid4().hex[:12]
    run.mkdir(parents=True, exist_ok=False)
    project = os.environ["PVSYST_EDITOR_PROJECT"]
    inverter = os.environ["PVSYST_EDITOR_INVERTER"]
    variant = os.environ.get("PVSYST_EDITOR_VARIANT", "VC0")
    # Refuse linked inputs so the snapshot cannot traverse outside the source.
    paths = list(source.rglob("*"))
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in paths):
        raise ValueError("Source workspace contains linked paths")
    before = {str(p.relative_to(source)): fingerprint(p) for p in paths if p.is_file()}
    (run / "source_manifest.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    evidence = {"cli_sha256": fingerprint(Path(os.environ["PVSYST_CLI"])),
                "project": project, "variant": variant, "inverter": inverter,
                "field": "PNomConv", "multiplier": 0.5,
                "start_date": os.environ.get("PVSYST_EDITOR_START", "2022.01.01"),
                "end_date": os.environ.get("PVSYST_EDITOR_END", "2022.01.02"),
                "runs": {}}
    print(f"Isolated verification: {run}", flush=True)
    for name in ("baseline", "edited"):
        root = run / name / source.name
        shutil.copytree(source, root, ignore=lambda directory, names: (
            [n for n in names if n in ("Results", "UserHourly", "UserBatch")]
            if Path(directory) == source else []))
        for folder in ("Results", "UserHourly", "UserBatch"):
            (root / folder).mkdir(exist_ok=True)
        store = ComponentStore(root)
        references = store.dependencies(project, variant)["references"]
        if not any(r["type"] == "OND" and r["name"] == inverter for r in references):
            raise ValueError("Selected project does not reference the requested inverter")
        info = store.inspect("OND", inverter)
        edited = None
        try:
            if name == "edited":
                nominal = float(info["fields"]["PNomConv"][0])
                args = {"expected_sha256": info["sha256"],
                        "updates": {"PNomConv": str(nominal * 0.5)}}
                preview = store.update("OND", inverter, dry_run=True, **args)
                (run / "preview.json").write_text(json.dumps(preview, indent=2), encoding="utf-8")
                edited = store.update("OND", inverter, **args)
                assert edited["sha256"] == preview["sha256"]
                evidence["component_before_sha256"] = info["sha256"]
                evidence["component_candidate_sha256"] = edited["sha256"]
            cli = PVsystCLI(os.environ["PVSYST_CLI"], root)
            sfi = build_sfi(root / "Models" / ("editor_" + run.name + ".sfi"), "energy")
            print(f"Running {name}...", flush=True)
            output = cli.run_simulation(project, variant, sfi=sfi,
                out_csv=root / "Results" / "curve_check.csv",
                start_date=evidence["start_date"], end_date=evidence["end_date"])
            csv = Path(output["csv"])
            summary = summarize(csv)
            columns, rows = parse_result_csv(csv)
            if len(rows) != 48 or not summary["complete"] or summary["energy_kwh"] is None:
                raise RuntimeError("Expected 48 complete hourly rows for this two-day verification")
            evidence["runs"][name] = {"csv": str(csv), "csv_sha256": fingerprint(csv),
                                       "energy_kwh": summary["energy_kwh"], "rows": len(rows)}
            print(f"{name}: {len(rows)} rows, {summary['energy_kwh']:.6f} kWh", flush=True)
        finally:
            if edited:
                restored = store.restore("OND", inverter, edited["backup_name"], confirm=True)
                assert restored["sha256"] == info["sha256"]
                evidence["restored_exactly"] = True
    (run / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    verify_outputs(evidence)
    after = {str(p.relative_to(source)): fingerprint(p) for p in source.rglob("*") if p.is_file()}
    assert before == after, "Source workspace changed during verification"
    evidence["source_unchanged"] = True
    (run / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2), flush=True)


if __name__ == "__main__":
    main()
