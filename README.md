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
data-factory --show-config    # resolve and print the active configuration
```

The fetch stage pages the locked scope into `data/raw/` one batch at a time
(`fetch_checkpoint.json` records progress), so Ctrl-C anywhere and re-running
continues where it stopped; re-running a completed fetch makes no HTTP requests.
The curate stage never drops data silently: contract violations go to
`data/curated/quarantine.jsonl` with reason codes, counts are printed every
run, and a quarantine ratio above `quarantine.max_ratio` fails the run (exit 5).

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
