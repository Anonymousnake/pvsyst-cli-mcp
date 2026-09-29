"""Optional CLI A/B check for one duplicated grid subarray; consumes two executions."""
import hashlib
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvsyst_cli import PVsystCLI, parse_result_csv
from pvsyst_components import ComponentStore
from pvsyst_variants import VariantStore


def main():
    cli = PVsystCLI(os.environ["PVSYST_CLI"], os.environ["PVSYST_WORKSPACE"])
    root = cli.workspace
    sfi = root / "Models" / "mcp_audit_energy.sfi"
    if not sfi.is_file():
        raise RuntimeError("Prepare mcp_audit_energy.sfi")
    project = f"MCP_Structure_{uuid.uuid4().hex[:12]}.PRJ"
    stem = project[:-4]
    csv_before = root / "Results" / f"{stem}_before.csv"
    csv_after = root / "Results" / f"{stem}_after.csv"
    if csv_before.exists() or csv_after.exists():
        raise FileExistsError("Verification output exists")
    store = VariantStore(root, ComponentStore(root), cli._lock)
    created = []
    backups = []
    try:
        result = store.clone_project("_Demo_PVsystCLI.PRJ", project)
        created = [root / "Projects" / project] + [
            root / "Projects" / f"{stem}.{variant}" for variant in result["variants"]
        ]
        initial = store.inspect(project, "VC0")
        if not store.validate_structure(project, "VC0")["valid"]:
            raise RuntimeError("Baseline structure is not self-consistent")
        before_result = cli.run_simulation(project, "VC0", sfi=sfi, out_csv=csv_before,
                                           start_date="2022.01.01", end_date="2022.01.02")
        modified = store.clone_subarray(project, "VC0", 1, initial["sha256"])
        backups.append(root / "Projects" / ".mcp-variant-backups" / modified["backup_name"])
        if len(store.inspect(project, "VC0")["subarrays"]) != 2:
            raise RuntimeError("Cloned subarray was not persisted")
        checked = store.validate_structure(project, "VC0")
        if not checked["valid"]:
            raise RuntimeError(f"Cloned structure is inconsistent: {checked['issues']}")
        after_result = cli.run_simulation(project, "VC0", sfi=sfi, out_csv=csv_after,
                                          start_date="2022.01.01", end_date="2022.01.02")
        metrics = []
        for path in (before_result["csv"], after_result["csv"]):
            columns, rows = parse_result_csv(path)
            if len(rows) != 48 or not {"EArray", "E_Grid"}.issubset(columns):
                raise RuntimeError("Missing 48-row energy output")
            metrics.append({name: sum(row[columns.index(name)] for row in rows)
                            for name in ("EArray", "E_Grid")})
        if any(not 1.5 < metrics[1][name] / metrics[0][name] < 2.5
               for name in ("EArray", "E_Grid") if metrics[0][name] > 0):
            raise RuntimeError(f"Subarray did not increase energy as expected: {metrics}")
        if hashlib.sha256(created[1].read_bytes()).hexdigest() != modified["sha256"]:
            raise RuntimeError("CLI unexpectedly rewrote the variant")
        removed = store.remove_subarray(project, "VC0", modified["subarray_id"], modified["sha256"])
        backups.append(root / "Projects" / ".mcp-variant-backups" / removed["backup_name"])
        if removed["sha256"] != initial["sha256"]:
            raise RuntimeError("Removal did not recover the exact baseline variant")
        print(f"CLI subarray A/B and removal: id={modified['subarray_id']}, {metrics}: OK")
    finally:
        for path in (csv_before, csv_after, *backups, *created):
            if path is not None:
                path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
