# Component commercial fields and remarks

`pvsyst_get_component` and `pvsyst_update_component` read and edit the commercial metadata block (`pvCommercial`) and multi-line `Remarks` array in text-format PVsyst 8.1.6 `.PAN`, `.OND`, `.BTR`, and `.GEN` files.

## Commercial fields (`commercial_updates`)

The `commercial_updates` dictionary accepts string values for up to ten fields on the direct `pvCommercial` object:
- Identity & provenance: `Manufacturer`, `Model`, `DataSource`, `YearBeg`, `PriceDate`
- Physical dimensions & packaging: `Width`, `Height`, `Depth`, `Weight`, `NPieces`

Behavior and edge cases when writing:
- **Missing fields**: If a supported field is not yet present in `pvCommercial`, the editor inserts it into the direct `pvCommercial` block (nested commercial sub-objects are not touched).
- **PAN dimension sync**: Older `.PAN` files also store module dimensions in root-level `LargApp` (width) and `LongApp` (length), whereas newer GUI exports often only write `pvCommercial.Width` and `pvCommercial.Height` (see [GUI PAN format](gui-pan-verification.md)). When updating `Width` or `Height` on a `.PAN`, the editor updates `LargApp`/`LongApp` if they already exist in the file, and leaves commercial-only files without legacy root fields as-is.
- **Root comment sync**: Updating `Manufacturer` or `Model` also updates the corresponding slots in the tab-separated root `Comment` header line while keeping its status and data-source columns intact.

## Multi-line `Remarks` array (`remarks`)

Pass `remarks` as a list of up to 5 strings:
- Providing 1 to 5 strings rewrites the `Remarks` block with synchronized `Count`, numbered `Remark_1`..`Remark_N` lines, and the closing marker.
- Passing an empty list `[]` removes the `Remarks` block from the file.
- Omitting `remarks` (or passing the exact existing lines) leaves the bytes untouched.

**Gotcha on `.PAN` files**: PVsyst sometimes stores functional flags on the 5th remark line (`Remark_5`) of a `.PAN` file. Keep that in mind before wiping or overwriting `Remarks` on an existing module.

Commercial updates and `remarks` can be combined with electrical scalar or Mode 1 curve updates in a single guarded `pvsyst_update_component` call.
