# Headless DC cabling (`pvsyst_create_dc_cabling_variant`)

`pvsyst_create_dc_cabling_variant` sets DC array wiring resistance (`mOhm`) or STC wiring loss (`%`) headlessly on `.VCx` variant files (PVsyst `8.1.0` / `8.1.6` / `8.1.7`) without opening or driving the PVsyst desktop GUI.

## Empirical PVsyst 8.1.6 / 8.1.7 model and flags

- **Array resistance at STC**: `R_array_stc_mohm = (NModSerie * Vmp_stc / (NStringCh * Imp_stc)) * 1000`, where `(Vmp_stc, Imp_stc)` are solved from the local `.PAN` file's 1-diode parameters (`GRef`, `TRef`, `Isc`, `Voc`, `Imp`, `Vmp`, `RSerie`, `RShunt`, `NCelS`, `NCelP`, `muISC`, optional `Gamma` / `IoRef`).
- **Conversion formula**: `RSCablagePC = 100 * R_mohm / R_array_stc_mohm` and `RSCablageCh = (loss_pct / 100) * R_array_stc_mohm`.
- **Flags written**:
  - `pvMainArray.Flags` sets `0x01800000` (DC wiring active).
  - `pvSubArray.Flags` sets `0x01800005` in `"percent"` mode and `(flags | 0x01800000) & ~0x00000005` in `"resistance"` mode.
  - Every `StringNode` in `pvCircuit` receives `CableDefinition=Start` / `Resistance=<R_mohm> mΩ` / `CableDefinition=End`.
  - Any stale `VarList` / `Histogram` result cache is stripped and root simulation bit `0x1` is cleared so `PVsystCLI` recomputes with the new resistance.

## Parameters and built-in module fallback

Pass `dc` with `mode` and `value`:
- `mode="resistance"`: global DC resistance (`mOhm`, non-negative finite number with `<= 2` decimals). Works for both local readable `.PAN` files and built-in encrypted/unreadable DB modules (written with `RSCablagePC=0.00` when `.PAN` is unreadable; `PVsystCLI 8.1.7` uses `StringNode` `Resistance`).
- `mode="percent"`: STC wiring loss (`%`, strictly `< 99.0%` matching PVsyst's 99% rejection guard). Supported when the referenced `.PAN` exists as a readable local file in the workspace; rejected when the module is an unreadable built-in DB component.
- `new_variant`: explicit target variant ID (e.g. `"VC1"`) that does not already exist in the project.
- `dry_run`: `dry_run=true` validates inputs, computes exact coupled values, and returns a unified diff without writing; `dry_run=false` writes the new `.VCx` atomically and verifies the saved structure and source SHA-256 preservation.
