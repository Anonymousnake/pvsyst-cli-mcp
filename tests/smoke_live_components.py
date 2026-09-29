"""Optional real CLI component-loading check; consumes one simulation execution.

Requires PVSYST_CLI and PVSYST_WORKSPACE with the vendor demo project
_Demo_PVsystCLI.PRJ / VC0 and the existing workspace component examples.
Every created component, variant and CSV has a unique name and is cleaned up.
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
    demo = root / "Projects" / "_Demo_PVsystCLI.VC0"
    sfi = root / "Models" / "mcp_audit_energy.sfi"
    if not demo.is_file() or not sfi.is_file():
        raise RuntimeError("Prepare _Demo_PVsystCLI.VC0 and mcp_audit_energy.sfi first")
    variants = set(cli.list_variants("_Demo_PVsystCLI.PRJ"))
    variant = next((name for name in ("VC9", "VC8", "VC7", "VC6") if name not in variants), None)
    if variant is None:
        raise RuntimeError("No available short verification variant")
    token = uuid.uuid4().hex[:12]
    module = f"MCP_Verify_{token}.PAN"
    inverter = f"MCP_Verify_{token}.OND"
    vc = demo.with_suffix(f".{variant}")
    csv = root / "Results" / f"mcp_component_verify_{token}.csv"
    created = [root / "ComposPV" / "PVmodules" / module,
               root / "ComposPV" / "Inverters" / inverter, vc, csv]
    if any(path.exists() for path in created):
        raise FileExistsError("Unique verification filename already exists")
    try:
        store.clone("PAN", "Generic_Mono_440W_Half.PAN", module,
                    {"Manufacturer": "MCPVerify", "Model": f"Mono {token}"})
        store.clone("OND", "Generic_9kW.OND", inverter,
                    {"Manufacturer": "MCPVerify", "Model": f"Inverter {token}"})
        content = demo.read_bytes()
        pairs = ((b"Generic_Mono_440W_Half.PAN", module.encode("ascii")),
                 (b"Generic_9kW.OND", inverter.encode("ascii")))
        for old, new in pairs:
            if content.count(old) != 1:
                raise RuntimeError(f"Expected one reference to {old!r}")
            content = content.replace(old, new)
        with vc.open("xb") as stream:
            stream.write(content)
        references = store.dependencies("_Demo_PVsystCLI.PRJ", variant)["references"]
        assert {item["name"] for item in references} >= {module, inverter}
        assert all(item["status"] == "workspace" for item in references
                   if item["name"] in (module, inverter))
        result = cli.run_simulation("_Demo_PVsystCLI.PRJ", variant,
                                    sfi=sfi, out_csv=csv,
                                    start_date="2022.01.01", end_date="2022.01.02")
        columns, rows = parse_result_csv(result["csv"])
        if not rows or "E_Grid" not in columns:
            raise RuntimeError("Simulation did not produce expected hourly energy values")
        print(f"CLI component verification: {variant}, {len(rows)} rows, "
              f"{len(columns)} columns, {result['seconds']} seconds: OK")
    finally:
        for path in reversed(created):
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
