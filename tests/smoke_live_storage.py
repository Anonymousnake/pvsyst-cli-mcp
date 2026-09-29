"""Optional real CLI BTR+GEN check; consumes one simulation execution.

Requires PVSYST_CLI and PVSYST_WORKSPACE containing the report's standalone
reference variant VC5 and the known-good user component fixtures.
New components, the temporary variant and result CSV are cleaned in finally.
"""
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvsyst_cli import PVsystCLI, parse_result_csv
from pvsyst_components import ComponentStore


def main():
    cli = PVsystCLI(os.environ["PVSYST_CLI"], os.environ["PVSYST_WORKSPACE"])
    root = cli.workspace
    store = ComponentStore(root)
    baseline = root / "Projects" / "_DEMO_StandAlone.VC5"
    sfi = root / "Models" / "ans_genset_audit.sfi"
    if not baseline.is_file() or not sfi.is_file():
        raise RuntimeError("Prepare _DEMO_StandAlone.VC5 and ans_genset_audit.sfi first")
    variants = set(cli.list_variants("_DEMO_StandAlone.PRJ"))
    variant = next((name for name in ("VC6", "VC7", "VC8", "VC9") if name not in variants), None)
    if variant is None:
        raise RuntimeError("No available standalone verification variant")
    token = uuid.uuid4().hex[:12]
    battery = f"MCP_Verify_{token}.BTR"
    generator = f"MCP_Verify_{token}.GEN"
    vc = baseline.with_suffix(f".{variant}")
    csv = root / "Results" / f"mcp_storage_verify_{token}.csv"
    created = [root / "ComposPV" / "Batteries" / battery,
               root / "ComposPV" / "Gensets" / generator, vc, csv]
    if any(path.exists() for path in created):
        raise FileExistsError("Unique verification filename already exists")
    try:
        for kind, source_name, target in (
            ("BTR", "Generic_Solar_12V160Ah.BTR", battery),
            ("GEN", "Generic_Diesel_50kVA.GEN", generator),
        ):
            source = root / "ComposPV" / ("Batteries" if kind == "BTR" else "Gensets") / source_name
            text = source.read_text(encoding="utf-8-sig")
            text = ComponentStore._replace_fields(kind, text, {
                "Manufacturer": "MCPVerify", "Model": f"{kind} {token}"})
            store.create(kind, target, text)
        content = baseline.read_bytes()
        pairs = ((b"Generic_Solar_12V160Ah.BTR", battery.encode("ascii")),
                 (b"Generic_Diesel_50kVA.GEN", generator.encode("ascii")),
                 (b"VBkUpEncl_syst=0.250", b"VBkUpEncl_syst=0.900"))
        for old, new in pairs:
            if content.count(old) != 1:
                raise RuntimeError(f"Expected one occurrence of {old!r}")
            content = content.replace(old, new)
        with vc.open("xb") as stream:
            stream.write(content)
        deps = store.dependencies("_DEMO_StandAlone.PRJ", variant)
        references = deps["references"]
        assert {item["name"] for item in references} >= {battery, generator}
        assert all(item["status"] == "workspace" for item in references
                   if item["name"] in (battery, generator))
        assert deps["generator_configuration"]["enabled_flag"]
        assert deps["generator_configuration"]["effective_backup_kw"] == 40
        result = cli.run_simulation("_DEMO_StandAlone.PRJ", variant,
                                    sfi=sfi, out_csv=csv,
                                    start_date="1990.01.01", end_date="1990.01.02")
        columns, rows = parse_result_csv(result["csv"])
        if not rows or not {"E_BkUp", "FuelBU", "BkUp_ON"}.issubset(columns):
            raise RuntimeError("Generator variables were silently omitted")
        active = sum(row[columns.index("BkUp_ON")] > 0 for row in rows)
        if not active:
            raise RuntimeError("Generator did not run in the selected interval")
        print(f"CLI BTR/GEN verification: {variant}, {len(rows)} rows, "
              f"{active} active generator rows, {result['seconds']} seconds: OK")
    finally:
        for path in reversed(created):
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
