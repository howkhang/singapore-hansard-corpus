# Normalized Hansard v1 — schema and export contract

Schema version: `hansard-normalized-v1`  
Parser version: `hansard-normalized-v1.1`

Hansard is legislative history, not enacted law. Normalized sections are source-traceable extracts. They are not propositions, policy signals, or proof of the later Act text.

## Compatibility

Consumers must reject unknown `schema_version` values. A later `hansard-normalized-v1.x` parser may add optional fields; it must not reuse section IDs for different spans. A `v2` schema is a breaking change.

This export is **not** identified by git HEAD. The working tree that produced it may be uncommitted. Identity is the SHA-256 inventory in `EXPORT_MANIFEST.json`.

## Snapshot hashing

- Each report file is canonical JSON: UTF-8, `sort_keys=True`, `indent=2`, trailing newline.
- `normalized_sha256` is SHA-256 of those file bytes.
- `source.sha256` is SHA-256 of the preserved local sitting JSON bytes. Historical files may have been reserialized; this hash is not necessarily the original HTTP body.
- Run timestamps live only in snapshot/export envelopes, not inside hashed report objects.

## Report identity

`report_id` = `sitting:YYYY-MM-DD`.

## Section identity

| Source | `section_id` | Stability |
|---|---|---|
| Legacy HTML span | `sitting:{date}:htmlspan:{start}` | `start` is a Unicode code-point offset into decoded `htmlFullContent` |
| Modern take | `sitting:{date}:takes:{i}` | `i` is the original `takesSectionVOList` index |
| Modern extras | `sitting:{date}:{collection}:{i}` | original list index (`attendance`, `ptba`, `a2b`, `annexure`, `vernacular`, …) |

IDs are stable for a triple `(source.sha256, schema_version, parser_version)`. They may change if the parser version changes.

## Locators

`normalized_text_sha256` is always SHA-256 of the exported `text` field. Consumers must not call producer `strip_html`.

| `unit` | Reconstruction |
|---|---|
| `htmlFullContent_unicode_offset` | `htmlFullContent[start:end]` |
| `json_string` | dotted `field` string `[start:end]` |
| `json_object` | UTF-8 JSON of the object at dotted `field` (see serialization below) |
| `json_fields` | concatenate each listed field slice in order |

Do not treat offsets as byte offsets into the JSON file. Section `text` is the full normalized passage, not an excerpt. Index `opening` fields may still be truncated for display.

### `json_object` serialization

`source_fragment_sha256` and `normalized_text_sha256` for `unit=json_object` are SHA-256 of the UTF-8 bytes of this serialization (no producer `strip_html`):

- JSON object, keys sorted lexicographically as Unicode strings
- `ensure_ascii=False` (non-ASCII code points as UTF-8, not `\uXXXX`)
- default separators: `", "` between items and `": "` between key and value
- no indent, no trailing newline, no extra spaces

Python equivalent:

```python
json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(", ", ": "))
```

Worked example: object `{"b": 1, "a": "café"}` serializes to exactly

```text
{"a": "café", "b": 1}
```

SHA-256 (hex) of those UTF-8 bytes:

`292eafeb3584e5e68d7ca7aa6780d68c22b47914aab0b9d3584b31acf8285632`

Compact separators (`","` / `":"`), pretty-print indent, or a trailing newline will not match.

## Classification statuses

- `classified` — proceeding type assigned from source structure.
- `unresolved` — text preserved; type could not be established.
- Relationship statuses on the debate index: `first_and_second_matched`, `second_reading_only`, `first_reading_only`, `title_variant_unconfirmed`, `explicit_resumption`.

Unknown capture times are `null`. Missing legislature fields use `{ "value": null, "origin": "absent" }`. Never invent modern equivalents.

## Unresolved content

If a boundary or type cannot be established, the text is kept as a section with `proceeding_type=unresolved` (or `sitting_front_matter` for leading chrome). Annexures and vernacular PDFs are recorded as metadata; bytes are not in the JSON (`attachment_bytes_not_in_source_json`).

## Failure behaviour

- Missing sitting, hash mismatch, or unsupported schema: nonzero exit; do not treat the export as complete.
- A failed normalize leaves the previous `normalized/v1` directory in place and writes `normalized/v1.staging` for inspection.

## Commands

```sh
python3 normalize_hansard.py --json-dir data/hansard/json --out-dir data/hansard/normalized/v1 normalize --dates 1955-05-25
python3 normalize_hansard.py normalize --all
python3 normalize_hansard.py validate
python3 normalize_hansard.py failures
python3 normalize_hansard.py retrieve sitting:1955-05-25:htmlspan:17659
python3 normalize_hansard.py export --export-dir data/hansard/export/hansard-normalized-v1
python3 build_second_readings_index.py
make check
```
