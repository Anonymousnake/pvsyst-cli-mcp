# Native DC Cabling Bridge

## Supported Entry

`pvsyst_create_native_dc_cabling_variant` drives the already open Chinese PVsyst 8.1.6 rev49278 GUI at DPI96. It uses the recorded detailed-loss window, resistance/STC-percent radio controls, native confirm and native SaveAs. PVsyst alone calculates the coupled subarray resistance/percent and every StringNode resistance. No proportional inversion, cable equation, irradiance or loss model is implemented here.

The operator must have the requested project open with DataPath equal to the MCP workspace. The tool does not start/exit PVsyst, edit INI, use the clipboard, patch private VC records or load another project. Dry-run reads only; input requires `dry_run=false` and `confirm=true`. GUI requests share the existing component-store lock with the house bridge within one server; operate one native GUI driver/server and leave the desktop alone during execution. This is not a cross-process desktop lock.

The source/candidate profile is native8.1.6, one ordinary fixed-plane/subarray PV-only Grid, complete consistent circuit counts, inactive AC, no MV/HV, load, storage, geometry, horizon or P50. It accepts only the recorded direct StringNode resistance shape with uniform values consistent with the subarray's stored 0.001mOhm summary. Custom/mixed/partial states stop. The native mode flags are exactly019E1002/resistance and019E1003/percent. Only this caller passes both subarray families to the shared scope validator; every existing editor retains its prior default019E1002 domain.

## Input And Persistence

`dc` has only `mode: resistance|percent` and strict numeric `value`. Resistance uses mOhm, percent uses STC loss percent. Adapter precision is exact0.01, finite/nonnegative, at most20 formatted characters, with percent<=100; these are adapter constraints, not measured manufacturer or all native GUI limits. Readback `dc_cabling` preserves native decimal strings for the subarray summaries and the higher-precision circuit resistance.

`variant_name` is a display title, not a requested VC id. Native SaveAs selects the new id; the response returns its actual path/id. Source PRJ/VC snapshots must remain unchanged before input, before save and after save. The actual saved candidate is parsed after its asynchronously written contents become ready; mode, exact requested value, owner consistency, display title and all non-DC physical records must agree. Only the owning DC subarray flag/Ch/PC, direct StringNode resistances and display comments/BOM may differ. Native saves can add UTF-8 BOM and re-encode a preexisting non-ASCII display comment from an unmarked source; physical lines remain exact after whitespace normalization. No source comment is rewritten externally.

A failed native action can leave the current modal for inspection. If SaveAs has already created a file, the response keeps `variant_created=true` and its path even when postchecks stop. No failed artifact is silently deleted. Saved state is not simulation-consumption certification.

## Fresh Native Evidence

A single parent-controlled GUI batch loaded the independent `DC_NATIVE_CONTROLS` project, saved an unmodified control, set45mOhm and2.5%, then re-edited percent back to45mOhm and resistance to2.0%. Each result was switched away from, reloaded from disk, and checked in the native GUI. Source PRJ/VC0 bytes remained unchanged. The owned process exited and only the original INI DataPath was restored, preserving unrelated native INI changes.

Six fresh native CLI runs used the same Hawaii SIT/MET, explicit2022 and SFI, each with8760 continuous finite hours and16 numeric columns. All source/new inputs remained unchanged. The original and GUI-unmodified control were identical in every result column; direct45mOhm and cross-mode re-edit45mOhm were also identical in every column.

| State | E_Grid kWh |
|---|---:|
| Original / native unmodified control | 799980.9366 |
| Native45mOhm / percent-to45mOhm | 798326.6995 |
| Native2.5% | 794847.5546 |
| Resistance-to2.0% | 797459.6545 |

The first CLI batch stopped during its third request after a long host pause produced a process-wait timeout; the two completed runs were retained, and only the four unfinished states were resumed with new output names. This is not relabelled a passed request. The old r26-r2 stale outputs are not the evidence for these new states. Exact GUI controls, requests, native CSV/PDF, source checks and failures are in local `native-migration-r58` records; commercial files are not committed.

## Acceptance Status

Public MCP headless read/preflight contracts pass with explicit `PVSYST_CONTRACT_WORKSPACE`; absent commercial fixture data skips rather than certifies execution. The specified static review route is `native-dc-review [magpie/mw/deepseek-v4.1-flash-sg]`. Its proposal to widen all existing editor defaults was rejected: the existing defaults must stay unchanged. Its claim of having made that edit was not present in the actual file on parent inspection. PRJ snapshot guards and exact basename checks remain; unsupported reviewer self-descriptions are not execution evidence.

Public live-GUI acceptance passed in one completed four-create/reload batch: read-only preview, explicit confirmation, both native modes, both cross-mode re-edits and the wrong-project stop were verified. All four public-created variants then ran fresh8760h/16-column native simulations and exactly matched every numeric GUI-reference column; E_Grid was798326.6995/794847.5546/798326.6995/797459.6545kWh respectively, with no warnings and unchanged source/new inputs. Records are `public3-preflight.json` and `public-dc-native.json`. Its first protected stop exposed asynchronous tab-control creation; the same readiness wait as the proved native batch was restored before binding controls. The second saved a disclosed variant but stopped on an invariant-mask off-by-one: object field line indices are zero-based, unlike circuit node boundary line numbers. The source/candidate allowed-field comparison was corrected without widening its field set; both failure records/artifacts remain. No whole-suite discovery, old smoke or hash tests were executed by the parent. Prior child hash/scope deviations are recorded separately and never credited as this phase's verification.
