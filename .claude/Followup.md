# Followup log

## 2026-09-26 — Smoke test: Tavily fetch → OKF write

**Tested:** Full extract→publish path for one source. Extracted
`https://advertising.amazon.com/API/docs/en-us/guides/overview` via the Tavily
MCP (`tvly extract`), then wrote the result as an OKF document per CLAUDE.md's
format, including the index and changelog updates.

**Result:** ✅ Working end-to-end.
- Basic extraction depth failed to fetch (page is JS-rendered); `--extract-depth advanced` succeeded (6,798 chars).
- Wrote `knowledge/amazon-ads-api-overview.md` (`id: amazon-ads-api-overview`, `status: official`, `confidence: high`), plus `knowledge/INDEX.md` and `knowledge/CHANGELOG.md`.
- Safe-to-re-run contract held: checked for an existing doc by `id` before writing (none — clean first write).

**Issues found:**
- `tvly extract --extract-depth advanced --timeout <n>` fails with a CLI validation error (`timeout: unexpected keyword argument`); advanced depth does not accept `--timeout`.
- Amazon docs pages require `advanced` extraction depth; basic always fails on this site.
