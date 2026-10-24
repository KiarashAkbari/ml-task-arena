# Stage 1 — Data Factory

Reproducible dataset factory for NYC 311 service requests (Term 3, Stage 1).
Challenge sources live in `files-given/`; the working contract for assistants is `AGENTS.md`.

## How to run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -e .

python -m unittest discover   # tests, offline
data-factory                  # whole pipeline (resumable; stages append over time)
data-factory fetch            # only the fetch stage
data-factory curate           # contract gate: quarantine + per-reason counts + threshold
data-factory clean            # cleaning + population join -> data/curated/cases.csv
data-factory outputs          # wide aggregate + run_manifest.json (with output hashes)
data-factory --show-config    # resolve and print the active configuration
```

The fetch stage pages the locked scope into `data/raw/` one batch at a time
(`fetch_checkpoint.json` records progress), so Ctrl-C anywhere and re-running
continues where it stopped; re-running a completed fetch makes no HTTP requests.
The curate stage never drops data silently: contract violations go to
`data/curated/quarantine.jsonl` with reason codes, counts are printed every
run, and a quarantine ratio above `quarantine.max_ratio` fails the run (exit 5).

## Population reference source (required before `clean`)

Choose a public ZIP → population table (NYC Open Data or Census; finding and
validating it is part of the challenge), normalize its header to

```csv
zip,population,borough
10001,25000,Manhattan
```

(all three columns required — `borough` feeds both the section 3 district
cross-check and the population rates of the aggregate table; county names like
`Kings` are normalized automatically),
save it as `data/reference/zip_population.csv`, commit it (it is small
reference data, not raw 311 dumps), and record its URL + fetch date in
`config/default.json` under `population.source_url` / `population.fetched_date`
for the report and manifest.

## Source field map

`source.field_map` in config binds canonical field names to the raw API field
names (e.g. `zip_code → incident_zip`). Confirm them with one request before
the first live run:

```bash
curl -sS 'https://data.cityofnewyork.us/resource/erm2-nwe9.json?$limit=1'
```

If a raw name differs, fix it in config — no code changes (AGENTS.md §1).

## Determinism check (AGENTS.md §4.5)

Two independent runs on the same window must list **identical `outputs`
hashes** in `run_manifest.json` (the manifest's own `started_at` /
`finished_at` are metadata and differ by design):

```bash
data-factory run && cp data/curated/run_manifest.json /tmp/run1.json
data-factory run   # fetch is a no-op; curate/clean/outputs re-emit identical bytes
python - <<'EOF'
import json, sys
a = json.load(open('/tmp/run1.json'))['outputs']
b = json.load(open('data/curated/run_manifest.json'))['outputs']
print('MATCH' if a == b else f'MISMATCH\n{a}\n{b}')
sys.exit(0 if a == b else 1)
EOF
```

## Configuration

All tunables live outside the code in `config/default.json` (source URL, locked
scope — agency + explicit complaint types —, date range, batch size,
paths, retry cap, backoff/timeout, quarantine threshold). Point to another file
with `--config PATH`. Unknown keys are rejected so typos fail fast.

## Exit codes

| code | meaning |
|------|---------|
| 0 | success |
| 1 | unspecified failure / unexpected bug |
| 2 | command-line usage error (argparse) |
| 3 | configuration error |
| 4 | network / HTTP error |
| 5 | data validation error |
| 130 | interrupted (SIGINT) |

Exact dependency pins live in `requirements.txt` (kept in sync with `pyproject.toml`).
Data directories (`data/raw/`, `data/curated/`) are created by the pipeline itself and are never committed.
