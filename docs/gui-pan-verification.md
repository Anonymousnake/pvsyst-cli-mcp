# GUI-exported PAN compatibility

Text8.1.6 PAN files can store dimensions in direct pvCommercial.Width/Height without legacy root LargApp/LongApp, and physical fields as Isc/muISC instead of ISC/MuISC. The editor recognizes these explicit aliases without rewriting them to a preferred spelling.

## Ownership and dimensions

Physical validation/edits use the module root; identity and form dimensions use direct commercial ownership. Similar names in metadata do not satisfy missing physical fields. Declared dimensions must be positive and finite; duplicate aliases or duplicate/ambiguous dimensions stop.

Commercial edits synchronize legacy dimensions only when those counterparts exist. They do not add legacy root fields to commercial-only exports. GUI-derived Gamma, Rp_0, temperature coefficients, rounding and technology conversions are not fitted or replicated.

## Remarks and native scope

Use the complete up-to-five-row Remarks array, not an unrelated single-line commercial field. The fifth PAN row may have functional meaning; see[commercial fields](commercial-verification.md). Preview, source guards, no-op and exact recovery preserve original spelling/BOM/newlines.

Supported GUI-format loading and partial width/remarks persistence do not prove independent physical effects of every scalar, GUI resaving of every MCP candidate or all four component-type round trips. OND GUI saving may recompute efficiency scalars and LastCompile; this is not permission to generalize its cache behavior. Other versions and technology branches need their own support evidence.
