"""Optional guarded SIT/MET update and restore check; consumes one CLI execution."""
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
    project = f"MCP_Source_{uuid.uuid4().hex[:12]}.PRJ"
    stem = project[:-4]
    csv = root / "Results" / f"{stem}.csv"
    sit = "Wanaluwawa.SIT"
    met = "Wanaluwawa_Nasa_SYN.MET"
    sfi = root / "Models" / "mcp_audit_energy.sfi"
    if (not all(path.is_file() for path in (root / "Sites" / sit, root / "Meteo" / met, sfi))
            or csv.exists()):
        raise RuntimeError("Missing SIT/MET/SFI fixture or result name collision")
    store = VariantStore(root, ComponentStore(root), cli._lock)
    created = []
    backups = []
    try:
        result = store.clone_project("_Demo_PVsystCLI.PRJ", project)
        created = [root / "Projects" / project] + [
            root / "Projects" / f"{stem}.{variant}" for variant in result["variants"]
        ]
        if result["variants"] != ["VC0"]:
            raise RuntimeError("Expected one source variant")
        initial = store.inspect_project(project)
        changed = store.update_project_sources(project, sit, met, initial["files"])
        backups.extend(root / "Projects" / ".mcp-variant-backups" / name
                       for name in changed["backups"].values())
        inspected = store.inspect_project(project)
        if inspected["warnings"] or inspected["files"] != changed["files"]:
            raise RuntimeError("Persisted source references failed inspection")
        output = cli.run_simulation(project, "VC0", sfi=sfi, out_csv=csv,
                                    start_date="1990.01.01", end_date="1990.01.02")
        columns, rows = parse_result_csv(output["csv"])
        if len(rows) != 48 or "E_Grid" not in columns:
            raise RuntimeError("Retargeted project produced unexpected hourly rows")
        header = csv.read_text(encoding="utf-8-sig", errors="replace").splitlines()[:8]
        if (not any(line.startswith("Geographical Site;Wanaluwawa.SIT;") for line in header)
                or not any(line.startswith("Weather data;Wanaluwawa_Nasa_SYN.MET;")
                           for line in header)):
            raise RuntimeError(f"CLI did not use the retargeted inputs: {header}")
        restored = store.restore_project_sources(project, changed["backups"], changed["files"], True)
        backups.extend(root / "Projects" / ".mcp-variant-backups" / name
                       for name in restored["backups"].values())
        if restored["files"] != initial["files"]:
            raise RuntimeError("Source restore did not recover the exact original bytes")
        print(f"Guarded source update, CLI and byte-identical restore: site={sit}, met={met}, rows={len(rows)}: OK")
    finally:
        csv.unlink(missing_ok=True)
        for path in created + backups:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
