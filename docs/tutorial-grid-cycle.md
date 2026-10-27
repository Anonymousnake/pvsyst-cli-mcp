# Native grid simulation workflow

Use existing tools to inspect inputs, create an isolated working project, run PVsyst and review actual hourly results/PDF. This does not provide all GUI editors, automatic electrical sizing or certification of the chosen equipment/site.

## Select inputs

Inspect the source project, variant parameters, references and structure. Keep original models/counts unless an explicit supported change is intended. unknown-or-builtin-db does not prove a missing model: native PVsyst may resolve its database. A same-named loose file can override it; do not replace a full database model with a short nameplate-only file merely for readable metadata.

simulate_template's fixed flow needs its supported single-subarray/no-active-shading shape and complete loose PAN/OND. Existing database-backed designs can instead use run_simulation with native model resolution. The separate inherited tracking task has its own restrictions.

Read MET root DateDebF/DateFinF, not its filename. Declared dates are not proof of payload coverage, timezone correctness or source-year authenticity. Generic1990, missing/unknown declarations and duplicate/inverted dates need the documented preflight/review handling, not silent recalendarization.

## Execute an isolated cycle

1. Inspect project/parameters/components and validate scoped structure; this does not replace electrical checks.
2. Clone the project into a new name, preserving original files.
3. Intentionally bind the workspace SIT/MET pair using update_project_sources and the complete current files map. Use the latest map for any later title/source transaction.
4. Build a new SFI with explicit variables such as all_common.
5. Run with an explicit native date interval and new CSV/PDF names. These illustrative names must exist in the workspace and the dates must match the actual weather:

```json
{
  "project":"GRID_DESIGN.PRJ","variant":"VC0","sfi_name":"GRID_EXPORT.SFI",
  "csv_name":"GRID_DESIGN_2024.csv","pdf_name":"GRID_DESIGN_2024.pdf",
  "site_name":"SITE.SIT","met_name":"SITE.MET","time_step":"hour",
  "start_date":"2024.01.01","end_date":"2024.12.31","report_language":"en","log_level":3
}
```

6. Read results and verify actual project/variant/SIT/MET context, units, finite values and consecutive hourly coverage:8760 or8784 records as applicable.
7. Review the native PDF's identities, orientations/counts, monthly data, main results and loss diagram. Compare energy with the report at its printed precision. Annual PR is not the arithmetic mean of hourly PR.

Output completeness or native success does not clear retained needs_review, clipping anomalies, missing source basis or weather warnings. Missing requested variables stay absent, not filled with zero. File transactions keep their own guarded reverse recovery; do not overwrite failed projects/results on retry.

Existing-scene recomputation through run_simulation is distinct from scene creation/editing and does not widen fixed_unshaded_grid. Further native capabilities and limits are in the[entry map](native-entry-map.md),[automatic task](automatic-simulation.md) and[engineering delivery](engineering-delivery.md).
