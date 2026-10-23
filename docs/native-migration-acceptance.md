# Approved Native Migration Acceptance

## Outcome

The original AI-authored residential PVCollada goal and the approved selective migration from private reference51148b9 are complete within the bounded entry domains below. These are separate accepted changes on the stable main line, not a bulk reference-branch merge. The DC proportional-resistance algorithm was deliberately excluded; the native GUI now owns DC setting, coupled recalculation and SaveAs. No replacement PV, cable, dispatch, tracking or shading model was added.

A fresh stdio MCP session after the final code merge exposes71 unique tools, including all native house/tracking/Shed/storage/AC/MV/DC entries. Existing tool arguments remain compatible. Restart a resident MCP process to load the added schema.

| Accepted Batch | Main Commit / PR | Scope And Evidence |
|---|---|---|
| Residential native chain | 1022a9c / PR63 | AI-authored necessary house parts and supporting-roof PV geometry; native import, orientation association, validity/shading table, new-variant persistence, reload and required simulation. [House workflow](house-pv-modeling.md) |
| Stored-state inspection | 91e7da0 / PR64 | Owner-aware native transformer/outage/peak/scene/ageing/IAM state; unknown forms remain explicit instead of implying edit support. [Read state](native-state-inspection.md) |
| Monthly PF/PNom closure | 32bdea7 / PR65 | Read/edit/re-edit/clear and six fresh native annual controls; no external cosine/reactive model. [PF evidence](power-factor.md) |
| Ageing validation | cac54b9 / PR66 | Whole native template gates retained around existing ageing controls; no external ageing/randomness implementation. [Entry map](native-entry-map.md) |
| Native report pages | cb8a4db / PR67 | Template/design report options and actual native annual reports; not merely persisted JSON. [Entry map](native-entry-map.md) |
| Tracking | ba793cc / PR68 | Nine recorded native tracking types, owned parameter precision, read/re-edit, source/candidate gates and fresh native annual CSV/PDF controls. [Tracking](native-tracking-controls.md) |
| Shed | 60825c9 / PR69 | Existing Sheds/SunShields source profiles, precision and repeated edits; native consumption verified without a replacement shading calculation. [Shed](native-shed-controls.md) |
| Storage scalars | b527c7b / PR70 | Strategy/hardware preserved, shared scalar semantics and Peak timing interoperability after edits; nine native48h cases, not annual evidence. [Storage closure](grid-storage-parameters.md#r55-原策略标量闭环) |
| MV night switch | 52f79b9 / PR71 | Single recorded default MV profile, two fresh8760h/16-column controls fully equal to GUI references; no arbitrary transformer/HV construction. [MV](native-mv-night.md) |
| AC cables | 932fb92 / PR72 | Coupled main/subarray/direct-inverter owners, exact0.1m and inherited conductor/section, public read/re-edit and unchanged StringNode DC. Cu100m annual fully equals GUI; four other cases48h only. [AC](native-ac-cabling.md) |
| Native DC bridge | 2e2bdf2 / PR73 | Confirmed already-open GUI mode/value controls; software computes all DC fields. Public preview/confirmation, two modes, cross-mode re-edit, read/reload, source/physical-record preservation; four public variants each8760h/16 numeric columns fully equal independent GUI controls. [DC](native-dc-cabling.md) |

## Verification And Preservation

Each code batch has its scoped public contract and native evidence in the linked phase records. Verification is not inferred from directory placement, symbol names or a preexisting project that happened to run. Owner/version/precision/count/source/candidate checks are kept explicit; old writer defaults are not globally widened merely to accept a new native flag family. Non-contract implementation experiments did not become permanent per-function test scaffolding.

The final DC boundary, AC and MV public contracts passed with the explicit local fixture workspace, alongside compilation and patch checks. The final readback confirms native percent2.00, subarray resistance49.216mOhm and circuit resistance49.2156564095741mOhm. Native produced these values; the adapter only verifies stored precision/ownership. GUI/native inputs, CSV/PDF, operation records and failures stay in the separate local case workspace, not the repository. Original projects, weather/models and the unrelated user file remain outside staged commits. Production concurrency/conflict protections remain; the parent ran no full discovery, old smoke or hash tests.

GUI/CLI/workspace/INI mutations were executed serially by the parent. The DC public GUI batch exited its owned process and restored the original DataPath; fresh native CLI runs finished, and a final process check found no PVsyst/PVsystCLI process. The library endpoints themselves do not start/exit the GUI or edit INI. Source/new-input byte checks are actual evidence, not a substitute for consumption verification. Desktop coordination is within the single-driver, process-local-lock contract, not a claim of OS-wide locking.

Failures and deviations were retained rather than relabelled passed: a wrong original MV source flag-family assumption, native startup/GUI-ready stops, a long-host-pause CLI timeout resumed only for unfinished cases, and DC asynchronous-control and zero-based-owner-mask fixes. Failed artifacts remain disclosed. Incorrect provider aliases initially returned null; the actual configured WorkBuddy SG provider is `magpie`, model `mw/deepseek-v4.1-flash-sg`. Independent review outputs carry that route. Reviewer proposals were adjudicated against actual code and public behavior, not adopted blindly. Self-reported off-whitelist sampling/hash/edit actions were separately recorded and excluded from acceptance credit; a claimed shared-default edit was absent on filesystem inspection and contradicted by the passing legacy-domain regression.

## Explicit Limits

This is completion of the approved bounded migration and original residential chain, not complete tutorial feature parity or general CAD. Furniture, interiors, fine hardware, surrounding vegetation/buildings and arbitrary CAD remain excluded. Native house import remains the recorded Chinese8.1.6 layout and one/two PV regions; broader standard export capacity is not claimed as native-import coverage.

Stored ageing/IAM/outage/transformer fields do not automatically gain unrestricted writers. Arbitrary topology, mixed cable/custom-resistance profiles, arbitrary transformer quantities/tables/HV, all native GUI locales/scales/versions, hybrid DC battery ports, grid-charging schedules, special/random loads/outages, automatic engineering sizing and unmeasured manufacturer constraints remain unpromoted. The complete tutorial entry map retains candidate/persistence/consumption/implementation distinctions. Forty-eight-hour evidence stays48h; it is never extrapolated to a year. SaveAs is not simulation certification. No unimplemented behavior is hidden by a synthetic result or external formula.
