# Component commercial fields

`pvsyst_get_component` returns commercial values, types, units and editability for text8.1.6 PAN/OND/BTR/GEN files. `pvsyst_update_component` accepts commercial_updates and remarks with the current expected fingerprint, optional preview and automatic backup. These edits do not fit or certify a physical model.

## Fields and ownership

The ten commercial fields are Manufacturer, Model, DataSource, YearBeg, Width, Height, Depth, Weight, NPieces and PriceDate. The input dictionary uses strings. Missing supported fields may be added to an existing direct pvCommercial object; nested commercial schemas are not modified. Identity changes belong in this dictionary, not arbitrary scalar updates.

PAN dimensions synchronize existing root LargApp/LongApp when present. Commercial-only GUI exports are accepted without inventing legacy counterparts; duplicate/ambiguous dimensions stop. Identity edits update manufacturer/model slots of a tab-separated root Comment while preserving its status/source slots. Other scopes, flags, curves and physical fields stay unchanged.

## Remarks and transactions

remarks replaces one complete array, including count, numbered rows and closing value. Up to five lines are supported; an empty list removes the array, omission preserves it, and a same-value array preserves bytes. Malformed/missing/duplicate headers, orphan or inconsistent rows/counts/closing values stop.

A PAN fifth remark line can have functional meaning; remarks are not universally physically inert. Preview, same-value no-op, stale-source checking, backup and restore use the normal component transaction. Commercial/remarks may share a transaction with supported electrical scalars or curves, subject to all their gates.

These fields do not implement seller-specific pricing, currencies or project economics, and successful native loading does not prove every field affects physics. PAN width/five-row GUI behavior is only partial GUI coverage; BTR/GEN and complete four-type GUI round trips are separate. See[PAN compatibility](gui-pan-verification.md) and[component creation](sourced-components.md).
