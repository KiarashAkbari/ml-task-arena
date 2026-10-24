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
data-factory --show-config    # resolve and print the active configuration
```

## Configuration

All tunables live outside the code in `config/default.json` (date range, batch size,
paths, retry cap, quarantine threshold). Point to another file with `--config PATH`.
Unknown keys are rejected so typos fail fast.

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
