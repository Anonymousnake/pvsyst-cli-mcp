# Grid circuit inventory

`pvsyst_validate_variant_structure` returns read-only grid_circuit diagnosis: groups, per-subarray declarations, expanded counts and separate declared_module_count/circuit_module_count. It does not repair files or certify physical sizing.

## Counts and ownership

Multiple unequal inverter groups may belong to one subarray. NInverter and NStringCh are checked against actual expanded branches; explicit_mppt_inputs inventories MPPT nodes, not physical port capacity or independent mode. For recognized independent layouts, declared NInvMPPT is compared directly with complete expanded inputs, not multiplied by inverter count. Saved nominal tags are not proof of effective native power.

Supported nodes are InjectionPoint, Transformer, Inverter, MPPT and String. Omitted NElements means one in the supported format; explicit values are positive32-bit integers. Reader limits are10,000 serialized nodes and64 stack levels, not native format limits. Nested cable/metadata fields do not provide node identity or inject children; declarations come from their owning subarray.

Malformed/duplicate counts, unknown references or declaration disagreements produce issues. Unknown nodes, unrecognized ownership and repeated transformer/injection multiplicities disclose incomplete scope rather than misleading aggregate totals. The bounded known shared-parent form has explicit member/parent ownership and deduplicated counts; mixed/unknown schemas remain unchecked.

valid isfalse for issues, null for unchecked scope only, true when implemented checks pass. Native CLI can load some inconsistent declarations, so successful simulation does not replace structural validation.

## Editing is separate

No diagnostic state automatically enables writes. Quantity, mode, sharing, orientation and copy/removal support use the full versions, geometry, ownership and transaction conditions in[topology editing](grid-capacity-verification.md). No automatic MPPT allocation, voltage/current sizing, scene synchronization or general merge/split follows from inventory totals.
