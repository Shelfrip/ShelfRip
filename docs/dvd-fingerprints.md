# DVD fingerprint compatibility

The proposed DVD recipe includes only files directly in `VIDEO_TS` whose extensions
are `.IFO`, `.BUP` or `.VOB` (case-insensitive). It preserves the existing filename
sort order and MD5 over little-endian 64-bit file sizes. Blu-ray hashing is unchanged.
This matches the file selection in the
[TheDiscDB specification](https://github.com/TheDiscDb/optical-disc-manifest/blob/main/specification/v1/README.md).

Previously, every file directly in `VIDEO_TS` contributed to the hash. DVDs containing
only standard files retain their hashes. DVDs with additional files receive different
hashes, on Windows as well as other platforms. A folder containing only unrelated
files no longer produces a DVD fingerprint.

`fingerprint()` returns the filtered `content_hash` and, only when different,
`legacy_hash` calculated from all files in `VIDEO_TS`. Local catalog and shared
lookups try the new hash before the legacy hash. The shared database then falls
back to the title-length signature if neither hash matches.

New rip metadata stores the new `content_hash`, retaining `legacy_hash` as a lookup
hint when needed. New catalog records and shared submissions use the new hash.
Existing catalog entries, saved rip metadata and shared-database records stay
unchanged and remain discoverable through the fallback. No migration is needed.
