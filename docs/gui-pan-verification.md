# GUI PAN format compatibility

Verified on 2026-09-30 using GUI/PVsystCLI 8.1.6. CLI SHA-256:
`e6526e0bb1b42cfcff62cba862ae54165dc55d89b1ba638a583cb7ae3bd46f4b`.

## Failure and correction

The earlier component validator required legacy root `LargApp`/`LongApp` and
exactly spelled `ISC`/`MuISC`. A normal GUI save produced commercial
`Width`/`Height`, omitted the legacy dimensions and spelled the physical fields
`Isc`/`muISC`. The resulting file was readable but received four missing-field
warnings, and create/clone/edit operations rejected it as incomplete. The form
also reported its width/height as non-editable.

The correction accepts each dimension from the commercial form or an existing
legacy root field, requires declared dimensions to be positive and finite, and
uses explicit aliases for the two physical field spellings. Input aliases edit
the existing key; the file is not rewritten to a preferred spelling. Physical
validation and edits use the root object, while form dimensions and identity
use the direct commercial object. Metadata with a similar name cannot satisfy
a missing physical field. Duplicate aliases or duplicate dimensions are rejected.

Commercial dimension edits synchronize legacy root counterparts only when
present. They do not add those fields to GUI-format exports. No GUI-derived
model parameters, rounding rules or technology conversions are implemented.

## Source evidence

DSH's local report, section 30, records an isolated GUI open/edit/save/reopen
sequence and file snapshots. The confirmed saved PAN sample is
`PAN_C5_guisaved.PAN` (1046 bytes), SHA-256:
`c973636a7e595653d5a0f9f6f9e9b2e5ecdc08564c31bfcd6a289469529af323`.
It contains `Isc`, `muISC` and commercial Width/Height, without LargApp/LongApp.
The report is external evidence; no vendor files or decompiled source are
distributed in this repository. Its behavior is specific to the recorded sample.

That GUI session also displayed five Remarks rows on the dimensions/technology
page and retained them when saving an edited width. The commercial page's
single-line field did not persist in those attempts, so it is not used as the
basis for the existing Remarks-array editor. OND file-mode points were retained
in a separate GUI test, while efficiency scalars and LastCompile were updated
by the GUI. Neither observation establishes all four component round trips.

## Verification

- 161 unit tests passed. The new GUI-format fixtures cover create/copy/clone,
  either alias spelling, retained serialization, missing/duplicate/invalid fields,
  object scoping, mixed scalar/form previews, no-op and byte-exact recovery.
- The real MCP lifecycle created and inspected the synthetic GUI-format PAN,
  edited legacy-named scalar inputs and commercial-only dimensions, checked
  preview hashes/stale rejection and restored the original bytes.
- A copy of the actual GUI sample above was placed in an independent copy of
  the source workspace. Through MCP, `ISC=11.100` and `MuISC=0.011` updated
  the existing `Isc`/`muISC` keys alongside commercial form edits. Its preview
  matched the applied SHA-256
  `ec6cf7dd4048355a5487a2d6ed8664ebcfd69e74efbbf2fb40da8f28055e1c5e`.
- PVsystCLI simulated `Wanaluwawa_50kWp.PRJ/VC0`, which references that PAN,
  for 2022-01-01–02. All 48 EArray rows were finite; 13 were positive. The
  component copy restored to the exact GUI sample hash. Both the prepared
  input copy and the original source manifest of 85 files remained unchanged.

Local evidence: `gui-pan-input-597e2b559a78` (input provenance) and
`mcp-commercial-1c178496f1ee` (MCP preview, native CSV, hashes and restoration).
The native test uses `tests/smoke_live_commercial.py`; provide a workspace copy
containing your GUI-exported PAN and a `PVSYST_COMMERCIAL_CASES` entry with
`components` and optional `scalar_updates` dictionaries. See that script and
[the commercial verification guide](commercial-verification.md) for environment
settings and the isolated-copy workflow.

This demonstrates loading the edited GUI-format file. It does not certify its
physical fit, prove each changed scalar/dimension's effect independently, or
reproduce the GUI's automatic changes to Gamma, Rp_0, temperature coefficients
and other derived fields. GUI resaving of the MCP-edited candidate remains a
separate check. Other export versions and chemistry/technology branches need
their own evidence.
