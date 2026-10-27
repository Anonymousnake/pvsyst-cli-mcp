# Standalone backup generator settings

Variant parameter inspection exposes generator; updates accept generator_updates for declared8.1.0/8.1.6 variants with exactly one pvSystem of SystemType=Battery. Runtime support is based on CLI8.1.6 loading those declarations, not every historical executable or GUI locale.

## Fields and requirements

The writer changes only system Flags bit0x20, GensetFile, PEffBackUp and explicitly selected subarray VBkUpEncl_syst/VBkUpDecl_syst. Missing supported generator fields may be inserted; nested objects, other bits, version, BOM and newlines stay unchanged.

Enabling requires an existing valid GEN, positive operating power and both thresholds for every subarray. Thresholds are native normalized voltages within[0,1], not SOC. No unevidenced start/stop ordering is imposed. Power above GEN nominal produces a warning rather than a sizing approval. Disabling retains saved settings; GEN nominal power and fuel parameters use a separate component transaction.

## Transactions and output

Use the current variant token. Preview does not write or create backups; same-value updates preserve numeric spelling. Apply snapshots original bytes and rechecks both variant and selected GEN, including a new loose file shadowing a builtin GEN. These are optimistic checks, not exclusion of external writers; recovery uses restore_variant.

Native controls cover48h dispatch, not full fuel/battery-model validation. BkUp_ON is fractional runtime, so a positive hourly row is not necessarily a full operating hour. Preserve E_BkUp/FuelBU/BkUp_ON units and actual omitted columns. Other system types, conversion, cross-file transactions and complete GUI save/reload are outside this entry.
