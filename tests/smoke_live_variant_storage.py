"""Optional real CLI test for BTR/GEN references in a managed variant clone."""
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
    project = "_DEMO_StandAlone.PRJ"
    sfi = root / "Models" / "ans_genset_audit.sfi"
    if not sfi.is_file() or "VC5" not in cli.list_variants(project):
        raise RuntimeError("Prepare _DEMO_StandAlone.VC5 and ans_genset_audit.sfi")
    variant = next((value for value in ("VC6", "VC7", "VC8", "VC9")
                    if value not in cli.list_variants(project)), None)
    if variant is None:
        raise RuntimeError("No unused VC6-VC9 variant identifier")
    token = uuid.uuid4().hex[:12]
    battery = f"MCP_Variant_{token}.BTR"
    generator = f"MCP_Variant_{token}.GEN"
    battery_path = root / "ComposPV" / "Batteries" / battery
    generator_path = root / "ComposPV" / "Gensets" / generator
    target = root / "Projects" / f"_DEMO_StandAlone.{variant}"
    csv = root / "Results" / f"mcp_variant_storage_{token}.csv"
    created = (battery_path, generator_path, target, csv)
    if any(path.exists() for path in created):
        raise FileExistsError("Unique verification output already exists")
    store = ComponentStore(root)
    variants = VariantStore(root, store, cli._lock)
    try:
        store.clone("BTR", "Generic_Solar_12V160Ah.BTR", battery,
                    {"Manufacturer": "MCPVerify", "Model": f"Battery {token}"})
        store.clone("GEN", "Generic_Diesel_50kVA.GEN", generator,
                    {"Manufacturer": "MCPVerify", "Model": f"Generator {token}"})
        variants.clone(project, "VC5", variant, {"BTR": battery, "GEN": generator})
        refs = variants.inspect(project, variant)["system_references"]
        assert refs["BTR"] == [battery] and refs["GEN"] == [generator]
        # The VC5 baseline keeps the backup off at 0.250; trigger it only in the disposable VC clone.
        data = target.read_bytes()
        old = b"VBkUpEncl_syst=0.250"
        if data.count(old) != 1:
            raise RuntimeError("Expected one baseline backup threshold")
        target.write_bytes(data.replace(old, b"VBkUpEncl_syst=0.900"))
        result = cli.run_simulation(project, variant, sfi=sfi, out_csv=csv,
                                    start_date="1990.01.01", end_date="1990.01.02")
        columns, rows = parse_result_csv(result["csv"])
        if len(rows) != 48 or not {"E_BkUp", "FuelBU", "BkUp_ON"}.issubset(columns):
            raise RuntimeError("Managed stand-alone variant omitted generator outputs")
        active = sum(row[columns.index("BkUp_ON")] > 0 for row in rows)
        if not active:
            raise RuntimeError("Generator did not activate in verified interval")
        print(f"CLI variant BTR/GEN clone: {variant}, {len(rows)} rows, {active} active: OK")
    finally:
        for path in reversed(created):
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
