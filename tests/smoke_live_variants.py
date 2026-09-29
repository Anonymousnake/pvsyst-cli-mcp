"""Optional live CLI check for managed variant cloning; consumes one execution."""
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
    project = "_Demo_PVsystCLI.PRJ"
    sfi = root / "Models" / "mcp_audit_energy.sfi"
    if not sfi.is_file() or "VC0" not in cli.list_variants(project):
        raise RuntimeError("Prepare _Demo_PVsystCLI.VC0 and mcp_audit_energy.sfi")
    variant = next((value for value in ("VC9", "VC8", "VC7", "VC6")
                    if value not in cli.list_variants(project)), None)
    if variant is None:
        raise RuntimeError("No unused VC6-VC9 variant identifier")
    token = uuid.uuid4().hex[:12]
    module = f"MCP_Variant_{token}.PAN"
    component = root / "ComposPV" / "PVmodules" / module
    target = root / "Projects" / f"_Demo_PVsystCLI.{variant}"
    csv = root / "Results" / f"mcp_variant_{token}.csv"
    if any(path.exists() for path in (component, target, csv)):
        raise FileExistsError("Unique verification output already exists")
    store = ComponentStore(root)
    variants = VariantStore(root, store, cli._lock)
    try:
        store.clone("PAN", "Generic_Mono_440W_Half.PAN", module,
                    {"Manufacturer": "MCPVerify", "Model": f"Mono {token}"})
        created = variants.clone(project, "VC0", variant, {"PAN": module}, 1)
        assert variants.inspect(project, variant)["sha256"] == created["sha256"]
        assert variants.inspect(project, variant)["subarrays"][0]["references"]["PAN"] == [module]
        result = cli.run_simulation(project, variant, sfi=sfi, out_csv=csv,
                                    start_date="2022.01.01", end_date="2022.01.02")
        columns, rows = parse_result_csv(result["csv"])
        if len(rows) != 48 or "E_Grid" not in columns:
            raise RuntimeError("Managed variant simulation returned unexpected results")
        print(f"CLI variant clone: {variant}, {len(rows)} rows, E_Grid present: OK")
    finally:
        for path in (csv, target, component):
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
