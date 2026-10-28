# GUI-exported PAN file compatibility

When PVsyst 8.1.6 GUI exports a `.PAN` module file to text, its field names and layout can differ slightly from older library `.PAN` files. `pvsyst_get_component`, `pvsyst_validate_component`, and `pvsyst_update_component` handle both formats transparently without rewriting the file's existing field spellings.

## Differences between legacy and GUI-exported `.PAN` files

1. **Module dimensions (`Width`/`Height` vs `LargApp`/`LongApp`)**:
   - Legacy `.PAN` files store module width and length at the root object level as `LargApp` and `LongApp` (in meters), alongside `pvCommercial.Width` and `pvCommercial.Height`.
   - Newer 8.1.6 GUI exports often omit `LargApp` and `LongApp` entirely and only write `Width` and `Height` inside the direct `pvCommercial` block.
   - The component reader and validator accept either layout (as long as dimensions are positive, finite, and unambiguous). When you edit `Width` or `Height` via `commercial_updates`, the writer updates `LargApp`/`LongApp` only if they are already present in the file, rather than injecting legacy fields into a commercial-only export.
2. **Short-circuit current case aliases (`ISC`/`MuISC` vs `Isc`/`muISC`)**:
   - Depending on how the `.PAN` file was saved, the root short-circuit current and its temperature coefficient appear as either `ISC` / `MuISC` or `Isc` / `muISC`.
   - The editor recognizes both spellings on read and preserves the file's original casing on write.

## Remarks and transactions

- Multi-line module notes are stored in the root `Remarks` array (up to 5 lines; see [commercial fields](commercial-verification.md)). Note that PVsyst uses the 5th line (`Remark_5`) in some `.PAN` files to store functional flags, so preserve it unless you intend to change those flags.
- Updating electrical or commercial fields in a `.PAN` file modifies only the requested scalars and preserves the original BOM, line endings, and single-diode model parameters (`Gamma`, `Rp_0`, `RSerie`, etc.).
