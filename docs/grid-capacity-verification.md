# Grid topology and parameter editing

The existing variant parameter, subarray copy/removal and structure tools support bounded grid layouts. They preserve unrequested models, cable metadata, losses and sizing hints. They do not select hardware, infer available MPPT ports, fit losses or certify electrical design. Native runtime evidence uses CLI 8.1.6; supported 8.1.0 declarations are not evidence of an 8.1.0 executable.

## Supported boundary

Count and structural edits require complete consistent declarations, circuit ownership, a fixed-plane assignment and one supported empty shading section. Empty flags are$00/$00020000/$00080000; active/unknown shading, scenes/tables, nested geometry or any PVObject_ModuleLayout block stop. Series/angle scalar edits alone may accept an absent shading section. No operation silently drops geometry or leaves stale module/string maps.

Selectors subarray_id, source_subarray_id and orientation_id are strict positive JSON integers: 1, not true, 1.0 or "1". Numeric values inside update dictionaries use their separate finite/integral range checks.

Preview writes nothing. Applying uses the current expected_sha256, backup and final source recheck. Same-value requests make no backup. Restore requires the current returned token and restores the actual snapshot, including mode/BOM/newlines. These are optimistic conflict checks, not cross-process locks or crash-atomic multi-file operations; coordinate external editors.

## Ordinary capacity edits

```json
{"subarray_updates":{"NInverter":12,"NStringCh":80}}
```

Ordinary sources require NInvMPPT=1 without independent bit$0400, no explicit MPPT branches or shared power, and all strings assigned to one plane. NInverter-only scales inverter groups while preserving strings per inverter. NStringCh-only scales parallel leaves while preserving inverter groups. Combined edits scale leaf multiplicity by(new_strings × old_inverters)/(old_strings × new_inverters), retaining relative allocation.

Every resulting group/leaf count must be integral. NElements, NInverter, NStringCh, NStrOrient1 stay synchronized. An omitted NElements represents one only while unchanged; changed multiplicities must already be explicit. This allocation does not imply proportional energy or refit AreaSizing, PNomSizing, resistance or other losses.

## Structure inspection

`pvsyst_validate_variant_structure` checks supported counted orientation lists, references, circuit ownership, aggregate inverter/string/module declarations and explicit MPPT inputs. Missing NElements means one only in the recognized format. valid isfalse for issues, null for incomplete checks, true only when scoped checks pass. Dome/unknown lists and unknown layouts remain explicitly unchecked.

Explicit MPPT nodes alone do not enable independent mode. For complete 8.1.0/8.1.6 independent$0400 records, NInvMPPT is compared directly with expanded explicit inputs, not NInverter × NInvMPPT. Whole/unknown mode or implicit allocation leaves this comparison unset. Successful native loading does not prove serialized count consistency. Saved PNomAC_InMPPT is not necessarily effective native model power.

A bounded shared parent requires homogeneous8.1.0 or8.1.6 member declarations, matching inverter references, canonical names, complete direct owned MPPT/string branches and group0 or all-1. Aggregate inverters deduplicate parents; unknown/mixed versions, groups or ownership do not produce misleading totals.

## Grouped subarray copy and removal

`pvsyst_clone_subarray` copies all complete exclusive groups of the selected subarray, preserving layout and relative quantities. Only owning IDs/names are remapped; fresh IDs exceed those already reserved across project variants. Ordinary removal deletes the owned root/groups only while another subarray uses its plane. Mixed/shared ownership, outside target references, unsupported systems or active geometry stop.

Complete explicit MPPT branches can be preserved without changing their mode. A drawn MPPT multiplicity is not independent mode or power sharing. Copying a complete branch is not arbitrary input redistribution.

## Fixed-plane lifecycle

clone_orientation=true appends a flat ordinary FixedPlane with Flags=$00 and no averaging, synchronizes NoOrient/NoOrientation and the list count. IDs must be sequential. Sources may be ordinary/no-explicit-MPPT, or recognized 8.1.0/8.1.6 independent$0400 exclusive direct-MPPT layouts with disabled sharing-1 and complete counts.

subarray_updates.NoOrientation reassigns a selected root to an existing supported plane. Every defined plane must retain a user; no orphan is silently pruned. Reference-only edits preserve list counts, geometry, topology and model/loss fields. Capacity values may accompany reassociation where their own scope permits; NInvMPPT resizing stays separate.

remove_orientation=true removes the root and its exclusively used plane only when another assigned plane remains. Higher plane IDs and all owning root references shift down by one for non-final deletion. orientation_id_map reports changed surviving IDs with string keys. Surviving root IDs, properties and quantities remain unchanged.

Flat/non-averaged mode, complete exclusive ownership and recognized-reference checks apply even to final-plane deletion. Every NoOrient*/OrientationId must belong to a counted plane or direct root NoOrientation; circuit or external references stop. Tracking, Dome, averaging, active scenes and shared cross-plane allocation are not rebuilt by these options.

## Independent MPPT quantity edit

NInvMPPT-only accepts integral1..10000 in known 8.1.0/8.1.6 independent$0400 layouts, explicit PowerSharingInverterId=-1, exclusive direct MPPT children, complete counts and one unshaded plane. MPPT group multiplicities scale; inverter and parallel-leaf quantities remain. NInvMPPT/NStringCh/NStrOrient1 synchronize. Changed NElements must exist and allocations must remain integral. Do not combine with NInverter, NStringCh or NoOrientation.

## Independent MPPT inverter count edit

NInverter-only scales each exclusive inverter-group multiplicity in the same known independent formats. MPPT/leaf multiplicities remain, while root NInverter, total NInvMPPT, NStringCh, NStrOrient1 synchronize. Changed inverter NElements must be explicit. Shared parents and simultaneous independent quantity edits stop. Declared inverter count is not a hardware-installation certificate; PVsyst determines model power.

## Independent parallel-string edit

NStringCh-only proportionally scales explicit leaves while keeping inverter/MPPT multiplicities, NInvMPPT and mode. Complete unshared ownership, fixed plane, explicit changed counts and integral allocation are required. Do not combine with independent input/inverter resizing. Unchanged resistance or power budgets can change clipping and losses; no exact energy scaling is promised.

## Explicit MPPT mode switch

independent_mppt_enabled is a separate strict-boolean transaction for complete known 8.1.0/8.1.6 exclusive direct-MPPT layouts with sharing-1. It changes only$0400 and root NInvMPPT: enabled uses expanded explicit inputs, disabled uses1. It preserves physical layout, quantities, other bits, models and cached losses/nominals. Mixed direct-string/MPPT, implicit or shared layouts and combined scalar/sharing changes stop. mppt_mode reports before/after.

## Existing power-sharing toggle

power_sharing_enabled selects a whole bounded parent through any member ID, not that member alone. Homogeneous recognized versions, identical inverter references, complete counts, common unshaded plane, no extra member parents/exclusive branches and no outside group0 references are required.

Enabling sets each member PowerSharingInverterId=0 and$0100; disabling sets-1 and clears only that bit. Mode$0400, topology, quantities and other fields remain. Native automatic allocation is used, not manual budget fitting. power_sharing reports all members and the parent. It does not merge arbitrary parents or combine with other scalar/mode edits.

## Shared member string edit

NStringCh-only can proportionally edit one member of a recognized bounded parent with group0 or all-1. Only its root string declarations and owned leaves change. Peer roots, input/inverter quantities, sharing IDs/bits, nominal tags and losses remain. capacity.shared_parent_line identifies this path. Shared inverter/input resizing is not supported; native effective budgets may rebalance without cached-tag changes.

## Shared-parent construction by clone

clone_subarray(..., share_inverter=true) copies an owned MPPT subtree under its existing inverter, not a new inverter. Requires recognized independent mode, sharing-1, one exclusive inverter/direct input of multiplicity1, complete counts and one common unshaded plane. The parent becomes owner0 with canonical combined name; both roots retain disabled sharing. Enabling remains a separate transaction. shared_parent_created reports this path. Do not combine with clone_orientation or arbitrary merging.

## Shared-member removal

remove_subarray(..., shared_member=true) removes either member of a complete disabled two-member parent, explicit owner0 and one inverter/input per member. The surviving root/quantities remain; parent ownership/name become exclusive to it. Outside target references stop. remaining_member_id reports the survivor. Do not combine with remove_orientation.

An active group must be explicitly disabled before removal. Restore deletion first, then the toggle to recover the active group. These are two guarded visible operations, not atomic deletion or implicit disabling. No general active budget redistribution or merge/split is implied.

## Series and angle scalars

NModSerie changes series count only. Ordinary angle edits require flat FixedPlane, Flags=$00 when present, and absent or uniquelyFalse UsesTiltAver/UsesAzimAver/UsesSlopeAver. Scalar legacy records may omit Flags; structural callers still require explicit$00. Unknown/duplicate modes, active averaging or geometry stop. No cold-Voc/MPPT sizing, scene synchronization or title refitting is performed.

## Loss scalars

Supported owning Grid declarations are8.1.0/8.1.6 without active geometry/layout. Fields must already exist uniquely; nested names/decoys are not edited. Inputs are strict finite numbers, not booleans or numeric strings.

| Input | Adapter range / mapping | Preserved state |
|---|---|---|
| subarray_updates.LIDLoss |0..100%, existing owning scalar|Flags, models, other losses|
| subarray_updates.PMismMPP |0..100%, clear only default bit0x00040000|Other bits, topology, components|
| subarray_updates.ModuleQualityLoss |[-5, 20]%, synchronize PQualityModU/I, clear only0x00100000|PQualityModP and other fields|
| main_array_updates.UFactor |15..100W/m²/K, one owning main Flags=$00100400/Flags2=$082A|Wind, absorptance, efficiency, components, topology|

QualityP, U, I are not independent percentage controls. Writing a mismatch scalar while its default bit remains set can leave it inactive. Updates disclose default_before/default_after and snapshots recover prior defaults. Adapter ranges are not universalGUI/factory limits; no native-default calculator or fitting is introduced. GUI loss saving can rewrite caches/modes outside the writer's scope; see [save/re-edit boundaries](loss-gui-roundtrip.md).

## Errors and process coordination

Expected input/file/PVsystError failures retain actionable MCP SDK2.x tool reasons. Dates use MM.DD, YY.MM.DD orYYYY.MM.DD, for example1990.01.01, not hyphenated dates. Unexpected internal exceptions remain distinct.

First CLI-client initialization is serialized inside the server; requests share one client and workspaceRLock. This is process-local, not an OS or external-editor lock. Native topology controls are bounded48h consumption evidence, not full-year energy accuracy, hardware sizing or arbitrary geometry equivalence.
