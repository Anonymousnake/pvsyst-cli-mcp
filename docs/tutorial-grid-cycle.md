# Tutorial-aligned native grid simulation cycle

This workflow follows the supplied 83-page Grid Connected Systems manual: project/site/weather (PDF pages 5 and 8-10), fixed orientation (11-12), array definition (17-21), and detailed losses (22-33). The manual and local models/results are not distributed in this repository.

The first acceptance target is a complete native simulation, hourly results and a readable native PDF. It is not parity with every GUI editor, automatic electrical design, or certification of the example site/equipment.

## Choose a usable source, not just a runnable file

Inspect a source project, its variant parameters, component references and structure before copying it. Preserve its component models and counts unless a deliberate, supported configuration change is requested.

`unknown-or-builtin-db` from `pvsyst_project_components` does not mean a component is absent: PVsyst can load it from its encrypted original database. A same-named loose workspace component takes precedence and may contain different or incomplete model parameters. Do not replace an original model with a short nameplate-only PAN/OND merely to make metadata inspection succeed.

`pvsyst_simulate_template` remains the bounded single-subarray/fixed-plane/no-active-shading flow for complete loose text PAN/OND files. For a valid existing design using original-database components, reuse `pvsyst_run_simulation`; the native program resolves the models. No component extraction or substitute PV model is needed.

Read the native MET root `DateDebF` and `DateFinF`, not the filename, to identify its declared calendar. The local demonstration named `Hawaii_2020_1min_ClipCorr.MET` declares 2022, as does the original CLI variant's simulation interval. Its name is not evidence of a 2020 simulation period or of measured source-year authenticity.

## Complete the cycle through existing tools

Use new project and output filenames for every replay. Keep original projects, components and weather files unchanged.

1. Use `pvsyst_inspect_project`, `pvsyst_get_variant_parameters`, `pvsyst_project_components` and `pvsyst_validate_variant_structure` on the selected source. Confirm the intended variant, orientation, series/parallel counts and source references. Structural validity is not electrical sizing certification.
2. Use `pvsyst_clone_project` to create the working PRJ/VC set. The accepted original-database example used `_DEMO_Commercial.PRJ`, variant `VC0`, retaining its fixed plane, 1000 modules and six inverters. Do not treat these demonstration values as a new project's requirements.
3. Bind the explicit workspace SIT/MET pair with `pvsyst_update_project_sources`, passing the complete `files` map returned by the clone as `expected_files`. If changing the display title, use `pvsyst_update_project_parameters` with the latest full file map. These transactions retain recovery backups.
4. Use `pvsyst_build_sfi` with a fresh filename and `variables="all_common"`. The accepted example exported hourly irradiation, array/grid power, PR and inverter losses.
5. Run the native tool with an explicit interval and fresh CSV/PDF names. The following are the accepted call's inputs; replace the output names on replay:

```json
{
  "project": "TUTORIAL_GRID_NATIVE01.PRJ",
  "variant": "VC0",
  "sfi_name": "TUTORIAL_GRID_NATIVE_EXPORT01.SFI",
  "csv_name": "TUTORIAL_GRID_NATIVE01_annual_2022.csv",
  "pdf_name": "TUTORIAL_GRID_NATIVE01_annual_2022.pdf",
  "site_name": "Hawaii_2020.SIT",
  "met_name": "Hawaii_2020_1min_ClipCorr.MET",
  "time_step": "hour",
  "start_date": "2022.01.01",
  "end_date": "2022.12.31",
  "report_language": "en",
  "log_level": 3
}
```

6. Use `pvsyst_read_results` to check finite samples, units, cadence and complete coverage. Verify the CSV lead-in identifies the actual working project, variant, SIT and MET. A normal year requires 8760 consecutive hourly rows; a leap year requires 8784.
7. Open the native PDF and check the project/variant, component identities, orientation/counts, main results, monthly table and loss diagram. Compare CSV energy with the rounded PDF total. Annual PR comes from the native report or the existing standard annual summary, not the arithmetic mean of hourly `PR`.

The accepted PDF explicitly identified both module and inverter as **Original PVsyst database** models. The native program performed the electrical and loss calculations; the adapter only configured files, invoked it and checked results.

## Accepted annual result

PVsystCLI 8.1.6, working project `TUTORIAL_GRID_NATIVE01.PRJ`, `VC0`, retained fixed tilt/azimuth 15/0 degrees:

| Check | Observed result |
|---|---|
| Native run | Completed without CLI warning lines; reported 10 seconds |
| Native calendar | 2022-01-01 00:00 through 2022-12-31 23:00 |
| Hourly completeness | 8760 consecutive rows, 60-minute cadence, complete finite samples |
| Native component source | Original PVsyst database, verified in PDF |
| Native system size | 440 kWp DC, six inverters / 360 kWac |
| Grid energy from hourly CSV | 799980.9366 kWh |
| Native PDF grid energy | 799981 kWh/year |
| Existing standard annual PR | 81.2162233% |
| Native PDF PR | 81.22% |
| Hourly IL_Pmax | 2991.3981 kWh; not total clipping including subhourly correction |
| PDF | Seven readable pages including monthly results, loss diagram and single-line diagram |
| Existing annual anomaly screen | No diagnostic flags |

This acceptance proves the basic native cycle with inherited demonstration inputs. It does not certify weather source-year authenticity, engineering suitability, climate extremes or optimal hardware sizing.

## Calendar preflight and retained failures

Annual template preflight now reads only the bounded native MET owning-field prefix. A declared date interval that cannot cover the requested calendar year stops the flow before cloning, creating outputs or starting a simulation. Duplicate root dates and inverted intervals are reported as issues. A complete generic-1990 declaration, absent dates or an unfamiliar date format remain explicitly disclosed rather than being silently re-calendarized. Root declarations are not proof of encoded-hour completeness, timezone correctness or climate quality; native execution and hourly-result checks remain required.

Two earlier attempts remain available locally as evidence:

- `TUTORIAL_GRID_CYCLE01` requested 2020 against MET dates declaring 2022. The old preflight passed, then the native CLI rejected the dates. The calendar guard addresses this false-ready result.
- `TUTORIAL_GRID_CYCLE02` used the matching 2022 calendar but existing loose Generic components. It produced all 8760 rows and a PDF, yet the existing anomaly screen correctly retained `needs_review` for extreme inverter clipping. No model, efficiency, native output or acceptance threshold was altered to make it pass.

The accepted original-database example is a separate working copy, not an overwrite or repair of either attempt.

## Remaining tutorial coverage

Existing load/storage tools and bounded loss/orientation edits are separate accepted capabilities, not proof that every tutorial setting has an exposed editor. Tutorial section 5.4 constant/monthly soiling now has a [native configuration and annual-verification path](soiling-verification.md). Remaining configuration work includes additional loss models, tracking/seasonal/dome/shed definitions, horizon import/editing, new 3D scene construction/editing/validation, peak shaving, inverter-temperature settings, power factor, numeric grid-power limits and P50/P90 configuration.

`pvsyst_run_simulation` can invoke the native program on an existing scene, including the supported shading-recomputation option. That is distinct from creating or editing a 3D scene; the scope limits of `pvsyst_simulate_template` and `pvsyst_simulate_design` apply to those bounded flows, not to every native simulation.

Complete each extension with native configuration/persistence/consumption evidence before exposing it. Do not fill missing GUI features with independent generation, shading or storage calculations.
