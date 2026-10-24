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
data-factory                  # CLI entry point
```

Exact dependency pins live in `requirements.txt` (kept in sync with `pyproject.toml`).
Data directories (`data/raw/`, `data/curated/`) are created by the pipeline itself and are never committed.
