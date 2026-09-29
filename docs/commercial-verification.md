# Commercial form verification

Verified with text component Version 8.1.6 and PVsystCLI 8.1.6 on 2026-09-30.
Implementation uses exported text structure and installed official help; no
decompilation is needed for this form. This does not certify GUI round trips.

## Contract

The existing `pvsyst_get_component` tool returns `commercial` with the values,
types, units and editability of ten fields: Manufacturer, Model, DataSource,
YearBeg, Width, Height, Depth, Weight, NPieces and PriceDate. Missing form fields
can be added to the existing `pvCommercial` object. Nested commercial objects
require a separate schema and are not changed.

The existing `pvsyst_update_component` tool takes `commercial_updates` and
`remarks`, with a required original SHA-256, optional dry run and automatic
backup. The field dictionary uses strings. It can share one transaction with
electrical scalar or supported curve edits. Identity fields must go in the
commercial dictionary when using the form. The 45 tool names remain unchanged.

PAN dimensions update both the commercial value and the existing root
LargApp/LongApp value. A missing/ambiguous root counterpart refuses that
dimension edit. Other object scopes, flags, curves and derived physical
parameters are preserved. Identity changes update a tab-separated root
Comment's manufacturer/model slots, retaining its status/source slots.

Remarks are replaced as one array, including count, row numbering and closing
value. Up to five lines are accepted; an empty list removes the array. Omission
preserves it. Arrays with missing/duplicate headers, mismatched counts, malformed
rows, orphan rows or an inconsistent closing value are refused. Same-value
arrays preserve bytes. A PAN fifth line can have functional meaning according
to the module help, so arbitrary notes are not assumed physically inert.

## Checks

- 117 offline unit tests pass, including four-type form edits, missing field
  insertion, array resizing/clearing, duplicate scopes, invalid values,
  Unicode line separators, stale/missing hashes, BOM/CRLF retention, PAN
  dimension synchronization and byte-exact restoration.
- The actual MCP stdio lifecycle passes for all four forms, with preview/apply
  SHA agreement, required-hash rejection, stale rejection and recovery. Existing
  OND/BTR curves, component management and project lifecycle smoke checks pass.
- `tests/smoke_live_commercial.py` uses the real MCP server and installed CLI
  in independent source-workspace copies. One native simulation per case:

| Referencing project/variant | Edited types | Dates | Result |
|---|---|---|---|
| Wanaluwawa_50kWp / VC0 | PAN, OND | 2022-01-01–02 | 48 hourly rows |
| _DEMO_StandAlone / VC5 | BTR, GEN | 1990-01-01–02 | 48 hourly rows |

The second case has its generator enabled and an effective backup power of
40 kW in the source variant. This verifies loading a configured GEN reference;
the test does not claim how much generator energy is dispatched.

Both cases edit identity/source fields, dimensions, weight, year, quantity,
date and three remark lines. Every preview hash matches the applied file.
The four component copies restore to their original SHA-256; all 85 source
files retain their hashes. CLI SHA-256 for this verification:
`e6526e0bb1b42cfcff62cba862ae54165dc55d89b1ba638a583cb7ae3bd46f4b`.

Local evidence is retained outside this repository under the verification
run `mcp-commercial-db771cc45716`: evidence.json, source_manifest.json,
per-component previews and native CSVs. No vendor component contents or
simulation outputs are committed here.

## Reproduction

Set `PVSYST_CLI`, `PVSYST_WORKSPACE`, `PVSYST_EDITOR_LAB` (outside the source
workspace), and `PVSYST_COMMERCIAL_CASES` to a local JSON file. Each case supplies
project, variant, two-day start_date/end_date, and a components dictionary
mapping type to existing referenced filename. Choose dates from that project's
MET, and use text 8.1.6 components with the dimension fields present for this
particular smoke script. Then run:

```powershell
python -m unittest discover -s tests -q
python tests/smoke_components.py
python tests/smoke_live_commercial.py
```

The last command consumes one native execution per case. It refuses linked
source paths, preserves source hashes, restores edited copies in `finally`
and keeps diagnostic evidence even if a run fails.

## Remaining limits

Native acceptance does not prove that every commercial field is used by the
GUI or economic calculation, nor that a field changes simulation physics.
There is no GUI reopen/save verification yet. PriceDate/NPieces do not implement
seller-specific prices, currencies or project economics. Other file versions,
legacy binary PAN and nested commercial object schemas need separate evidence.
See [the capability roadmap](gui-capability-roadmap.md) for the remaining
non-decompilation work and the specific reverse-engineering topics.
