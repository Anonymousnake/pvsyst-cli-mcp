# Step-by-step grid simulation workflow

When you want full manual control over each step instead of calling the all-in-one `pvsyst_simulate_template` or `pvsyst_simulate_design` workflows, you can chain the low-level inspection, project cloning, weather binding, `.SFI` generation, and `pvsyst_run_simulation` tools directly. This is especially useful when working with existing multi-subarray projects or projects that reference PVsyst's built-in encrypted component database.

## 1. Inspect the source project and weather

Start by checking the project structure, component references, and `.MET` calendar coverage:
- Call `pvsyst_inspect_project`, `pvsyst_get_variant_parameters`, `pvsyst_project_components`, and `pvsyst_validate_variant_structure`.
- If `pvsyst_project_components` reports a component source as `unknown-or-builtin-db`, that simply means the `.PAN` or `.OND` is not a loose file in `ComposPV`—`PVsystCLI.exe` will still resolve it from PVsyst's built-in database at runtime. Be careful not to drop a stub loose file with the same filename into `ComposPV`, as PVsyst will let the loose file shadow the built-in model.
- Check the `.MET` root `DateDebF` and `DateFinF` headers rather than trusting a year in the `.MET` filename. Note that synthetic or generic weather files often carry `1990` in their headers.

## 2. Clone, configure, and run

1. **Clone an isolated copy**: Call `pvsyst_clone_project` to create a working project so you never mutate your baseline `.PRJ`/`.VCx` in place.
2. **Bind site and weather (optional)**: If you are switching weather files, call `pvsyst_update_project_sources` with the current `files` SHA-256 map from `pvsyst_inspect_project`.
3. **Create an `.SFI` export definition**: Call `pvsyst_build_sfi` (for example with `variables="all_common"`) to tell PVsyst which hourly columns to write into the result CSV.
4. **Run the simulation**: Call `pvsyst_run_simulation` with explicit dot-formatted dates (`YYYY.MM.DD`) matching the `.MET` file's calendar year and fresh `csv_name` / `pdf_name` targets:

```json
{
  "project": "GRID_DESIGN.PRJ",
  "variant": "VC0",
  "sfi_name": "GRID_EXPORT.SFI",
  "csv_name": "GRID_DESIGN_2024.csv",
  "pdf_name": "GRID_DESIGN_2024.pdf",
  "site_name": "SITE.SIT",
  "met_name": "SITE.MET",
  "time_step": "hour",
  "start_date": "2024.01.01",
  "end_date": "2024.12.31",
  "report_language": "en",
  "log_level": 3
}
```

If the variant already contains a 3D shading scene and you want PVsyst to rebuild its shading factor tables before simulating, pass `recompute_shading=true`.

## 3. Read and verify outputs

Call `pvsyst_read_results` (and `pvsyst_read_rows` if you need specific hourly slices):
- Check that the CSV header matches the expected project, variant, `.SIT`, and `.MET` filenames, and that the row count covers the full year (`8760` hours, or `8784` in a leap year).
- When comparing annual Performance Ratio (PR) against the PDF report summary, compute PR from the annual sums ($\sum E_{\text{Grid}} / (\sum \text{GlobInc} \times P_{\text{nom}})$) rather than averaging the hourly `PR` column.
