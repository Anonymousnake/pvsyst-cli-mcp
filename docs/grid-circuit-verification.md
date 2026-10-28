# Grid circuit tree inventory

When you call `pvsyst_validate_variant_structure`, its `grid_circuit` field returns a read-only breakdown of the serialized electrical circuit tree inside a `.VCx` variant file, comparing what the subarray headers declare against what is actually expanded in the node tree.

## How the circuit tree is parsed

In PVsyst 8.1.0 and 8.1.6, the electrical layout is stored as a stack of nested nodes (`InjectionPoint`, `Transformer`, `Inverter`, `MPPT`, and `String`), where each node carries an optional `NElements` multiplicity (defaulting to `1` when omitted) and an owner reference back to its subarray.

The parser walks up to 10,000 nodes (up to 64 nesting levels) and checks:
- **Subarray inverter and string totals**: A single subarray can own multiple inverter groups with different string counts. The validator expands `NElements` down each branch and compares the expanded totals against the subarray header's `NInverter` and `NStringCh`, as well as comparing `declared_module_count` (`NStringCh × NModSerie`) against `circuit_module_count`.
- **Independent MPPT inputs (`explicit_mppt_inputs`)**: Counts explicit `MPPT` nodes in the tree. When a subarray has independent MPPT mode enabled (`Flags` bit `$0400`), its header `NInvMPPT` is checked directly against the total expanded MPPT inputs (not `NInverter × NInvMPPT`).
- **Shared inverter parents**: Recognizes two-member shared inverter groups (`Owner=0` on the parent inverter with `PowerSharingInverterId` `0` or `-1` on the member subarrays) and deduplicates shared parent inverters when reporting whole-project inverter counts.

## Understanding `valid`

- `valid: true`: All orientation references, subarray headers, and circuit tree counts match.
- `valid: false`: Found a broken reference, malformed/duplicate count, or a mismatch between subarray header declarations and the expanded circuit tree (listed in `issues`). Note that `PVsystCLI.exe` will sometimes load and simulate a file even when header counts and circuit nodes disagree, so running this check catches silent topology drift early.
- `valid: null`: No hard errors were found, but the variant uses node types, Dome orientations, or multi-transformer structures outside the validator's full rule set.

To modify subarray quantities, MPPT modes, or shared inverter groups, see [grid topology and parameter editing](grid-capacity-verification.md).
