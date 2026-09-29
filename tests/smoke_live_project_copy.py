"""Optional live CLI check for copied projects; consumes one execution."""
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
    project = f"MCP_Copy_{uuid.uuid4().hex[:12]}.PRJ"
    csv = root / "Results" / f"{project[:-4]}.csv"
    if csv.exists():
        raise FileExistsError(csv)
    store = VariantStore(root, ComponentStore(root), cli._lock)
    created = []
    backups = []
    try:
        result = store.clone_project("_Demo_PVsystCLI.PRJ", project)
        created = [root / "Projects" / project] + [
            root / "Projects" / f"{project[:-4]}.{variant}" for variant in result["variants"]
        ]
        if project not in cli.list_projects() or "VC0" not in cli.list_variants(project):
            raise RuntimeError("Copied project or VC0 not visible to CLI adapter")
        initial = store.inspect_parameters(project, "VC0")
        updated = store.update_parameters(project, "VC0", initial["sha256"], 1, 1,
                                          {"NModSerie": 15}, {"FieldTilt": 25})
        backups.append(root / "Projects" / ".mcp-variant-backups" / updated["backup_name"])
        if store.inspect_parameters(project, "VC0")["sha256"] != updated["sha256"]:
            raise RuntimeError("Parameter edit was not persisted")
        output = cli.run_simulation(project, "VC0", sfi=sfi, out_csv=csv,
                                    start_date="2022.01.01", end_date="2022.01.02")
        columns, rows = parse_result_csv(output["csv"])
        if len(rows) != 48 or "E_Grid" not in columns:
            raise RuntimeError("Copied project simulation returned unexpected results")
        print(f"CLI project copy: {project}, {len(rows)} rows, E_Grid present: OK")
    finally:
        csv.unlink(missing_ok=True)
        for path in created + backups:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
