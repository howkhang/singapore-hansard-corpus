# Singapore Hansard corpus

Sitting reports of the Singapore Parliament (and earlier Legislative Assembly) from **22 April 1955** to **7 July 2026**, plus a candidate Second Reading index.

Hansard is legislative history, not enacted law. A ministerial speech does not prove the later Act text, Parliament’s collective intention, or that a court would give the passage weight. Use it with the enacted provision and Singapore interpretation rules (Interpretation Act 1965 s 9A).

Requires **Python 3.9+**.

## Get the reports

Clone the repo, then unpack the checksummed shards (this verifies SHA-256 first):

```sh
python3 -m pip install -r requirements.txt
python3 pack_hansard_shards.py unpack
```

Raw sittings land in `data/hansard/json/YYYY-MM-DD.json`. Two payload shapes appear:

- **modern** — `metadata` + `takesSectionVOList` (from about 2012)
- **legacy** — `htmlFullContent` HTML (1955–early 2010s)

## Share / rebuild shards

```sh
python3 pack_hansard_shards.py pack
python3 pack_hansard_shards.py verify
```

Packing writes a staging directory, verifies it, then replaces `data/hansard/shards/` only on success. Archives use fixed tar/gzip metadata so identical inputs hash identically. Checksums are in `data/hansard/shards/SHA256SUMS`.

## Recrawl missing sittings

```sh
python3 parl_crawler.py sitting_dates.txt
```

Existing files are skipped only if they still validate as a Hansard report for that date. Error JSON and empty files are not treated as success. Any failed date exits non-zero.

## Normalized layer

Both source formats convert to `hansard-normalized-v1` reports:

```sh
python3 normalize_hansard.py normalize --all
python3 normalize_hansard.py validate
python3 normalize_hansard.py retrieve sitting:1955-05-25:htmlspan:17659
python3 normalize_hansard.py export
```

Contract: `docs/normalized-hansard-v1.md`. Consumer import: `docs/CONSUMER_HANDOFF.md`.

## Second Reading index

```sh
python3 build_second_readings_index.py
```

Writes `data/hansard/second_readings_index.json` and `.md`. Continuations are linked only when the later sitting has explicit resumption language (“Question again proposed”, “Debate resumed”, “Resumption of Debate”). Distinct Bills with the same short title stay separate. Locators include section index or HTML offset and a passage SHA-256. Bill-to-Act identity remains a candidate until checked against the enacted text.

## Check

```sh
make check
```

Runs fixture tests for both report formats and checks that index coverage counts match the entries.
