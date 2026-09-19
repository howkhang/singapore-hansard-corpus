# Consumer handoff (singapore-law-substrate)

Import **exported artifacts**, not this repository’s Python parsers.

## What to read

1. `docs/normalized-hansard-v1.md` — schema and locator rules.
2. `data/hansard/export/hansard-normalized-v1/EXPORT_MANIFEST.json` — content-addressed inventory.
3. `data/hansard/normalized/v1/reports/YYYY-MM-DD.json` — one report per sitting.
4. `data/hansard/second_readings_index.json` — candidate debate index (`parser_version` `second-readings-index-v7`) with `section_id` locators.
5. `data/hansard/export/hansard-normalized-v1/conformance/` — expected records and invalid examples.

Do **not** import `hansard_norm.legacy` or reproduce HTML marker regexes. Passage retrieval is: load report → select `section_id` → verify hashes.

## Import steps

1. Verify `EXPORT_MANIFEST.json` `schema_version` is `hansard-normalized-v1`. Reject anything else.
2. For each inventory row, hash the report file with SHA-256 of canonical UTF-8 JSON (`sort_keys`, indent 2). Reject on mismatch.
3. Store reports under the substrate’s hash-addressed Hansard cache, recording `source.sha256` separately from any live HTTP capture.
4. Index `section_id` plus `proceeding_type`. Treat `second_reading_*` labels as mechanical candidates, same reliability policy as `sources/hansard/README.md`.
5. On retrieve: load section and check `sha256(section.text) == locator.normalized_text_sha256`. Reconstruct the source fragment from `locator.unit` (HTML slice, JSON string slice, JSON object, or concatenated fields) and check `source_fragment_sha256`. For `json_object`, serialize as specified in `docs/normalized-hansard-v1.md` (UTF-8, `ensure_ascii=False`, sorted keys, separators `", "` / `": "`, no trailing newline). Do not import producer `strip_html`.

## Provenance

`capture.captured_at` is null unless a `.meta.json` sidecar exists. Historical JSON hashes are preserved local bytes, not a claim of original HTTP bytes.

Git HEAD must not be used as the snapshot id while the producer working tree is uncommitted.

## Out of scope for the consumer

- Recrawling SPRS.
- Parsing `htmlFullContent`.
- Promoting speeches to law or PolicySignals.
- Assuming Bill-to-Act identity from the debate index without checking enacted text.
