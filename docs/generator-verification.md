# Standalone generator configuration verification

Verified on 2026-09-30 using PVsystCLI 8.1.6. This workflow uses GUI-exported
text fields and installed official help; it does not need decompilation.

## Contract and scope

The existing variant parameter tools now expose a `generator` form and accept
`generator_updates`. Supported variant versions are 8.1.0 and 8.1.6, with one
`pvSystem` of `SystemType=Battery`. The writer changes only system Flags bit
`0x20`, GensetFile, PEffBackUp and explicitly selected subarray VBkUpEncl_syst /
VBkUpDecl_syst fields. Missing generator fields can be inserted. Nested objects,
other flags, file version, BOM and line endings are preserved.

Enabling requires a valid existing GEN, positive operating power, and both
thresholds in all subarrays. Thresholds use native normalized voltage units
in [0,1], not SOC. Start/stop ordering is not constrained without evidence of a
native constraint. Operating power above GEN nominal power produces a warning;
native acceptance of that value is not evidence of appropriate system sizing.
Disabling preserves the stored configuration. Nominal power and fuel settings
remain GEN component fields, edited in a separate transaction.

The variant SHA-256 is required. Dry run returns a bounded diff and candidate
hash without a backup or write. Applied edits save exact original bytes; no-op
generator edits retain the original numeric spelling and create no backup.
Before replacement, the variant and selected GEN are checked again, including
new workspace files that would shadow a builtin GEN. These checks do not
exclude external processes in the final read/replace interval. Restore uses
the existing variant recovery API.

## Native controls

`tests/smoke_live_generator.py` created two independent source-workspace copies
per variant. Both copies received the same existing 50 kW nominal GEN, 40 kW
operating power, and thresholds 0.9/0.95 on every subarray. Only the enabled flag
differed. The thresholds deliberately induce activity in the test period and
are not recommended design settings.

| Source variant | File Version | Initial configuration | Disabled | Enabled |
|---|---|---|---|---|
| _DEMO_StandAlone / VC0 | 8.1.0 | GEN filename and operating power absent | 48 rows; no generator columns | 48 rows; 21 rows with BkUp_ON > 0 |
| _DEMO_StandAlone / VC5 | 8.1.6 | GEN filename/power present | 48 rows; no generator columns | 48 rows; 21 rows with BkUp_ON > 0 |

Dates: 1990-01-01 through 1990-01-02, hourly. Each enabled run produced finite,
nonnegative E_BkUp, FuelBU and BkUp_ON with positive sums. Timestamps, GlobInc and
GlobEff match the corresponding disabled control. The sums in both enabled
cases were E_BkUp=24.6006 (hourly kW samples), FuelBU=0.1231 liter and BkUp_ON=
0.6521 Hour. **21 active rows do not mean 21 hours of runtime**: BkUp_ON is a
fractional duration. These observations establish dispatch, not independent
validation of the internal fuel or battery model.

Every applied hash matched its MCP preview. All four edited variant copies
restored exactly. All 85 source files retained their hashes. After the final
implementation changes, newly generated previews independently matched all
four native-tested candidate hashes and the retained CSV hashes were verified.

CLI SHA-256:
`e6526e0bb1b42cfcff62cba862ae54165dc55d89b1ba638a583cb7ae3bd46f4b`.

Local evidence runs outside the repository:

- `mcp-generator-7bddb2bad74e` (VC0 / file 8.1.0)
- `mcp-generator-293a6f184767` (VC5 / file 8.1.6)

Each retains evidence.json, source_manifest.json, preview JSON and native CSVs.
An earlier attempt (`mcp-generator-8a286bf3b04a`) stopped before writing because
the initial implementation supported only file 8.1.6; 8.1.0 was then added and
tested without changing the source version. Vendor files and simulation outputs
are not committed.

## Automated checks and reproduction

129 offline unit tests pass. Generator coverage includes missing fields,
enable/disable, multi-subarray requirements, nested decoys, duplicate fields,
unsupported versions/system types, strict values, no-op byte preservation,
stale and during-backup changes, GEN dependency changes, builtin shadowing,
preview and exact restoration. The real 45-tool MCP stdio lifecycle passes,
including typed generator settings and all existing component/project flows.

```powershell
python -m unittest discover -s tests -q
python tests/smoke_components.py
```

For opt-in native verification, set PVSYST_CLI, PVSYST_WORKSPACE,
PVSYST_EDITOR_LAB (outside source), PVSYST_EDITOR_PROJECT and
PVSYST_EDITOR_GENERATOR. Select PVSYST_EDITOR_VARIANT (default VC0) and, if
needed, PVSYST_EDITOR_START / END for a valid two-day period and
PVSYST_EDITOR_GENERATOR_KW (default 40). Then run:

```powershell
python tests/smoke_live_generator.py
```

This consumes two native simulations per variant, restores copies in `finally`
and verifies source hashes. Other file versions, GUI reopening/saving,
cross-file transactions, system-type conversion and complete physical design
checks remain outside this verified scope. See the [capability roadmap](gui-capability-roadmap.md)
for remaining non-decompilation work and reverse-engineering topics.
