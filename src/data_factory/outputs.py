"""Outputs stage: aggregate table + run manifest (AGENTS.md section 4.5).

Runs after clean; produces outputs 2 and 3 (output 1, the case dataset, is
what ``clean`` writes). The manifest cross-checks that curated rows equal
gate-passing records before publishing hashes — outputs are never reported
as consistent when they are not.
"""

from __future__ import annotations

from dataclasses import dataclass

from data_factory.aggregate import AggregationResult, run_aggregate
from data_factory.config import Settings
from data_factory.manifest import collect_manifest, write_manifest


@dataclass(frozen=True)
class OutputsResult:
    aggregate: AggregationResult
    manifest_path: str
    output_hashes: dict[str, str]  # file name -> sha256 (from the manifest)


def run_outputs(settings: Settings, *, started_at: str, finished_at: str) -> OutputsResult:
    """Build the aggregate table, then hash everything into the manifest."""
    aggregate = run_aggregate(settings)
    manifest = collect_manifest(settings, started_at=started_at, finished_at=finished_at)
    manifest_path = write_manifest(settings, manifest)
    return OutputsResult(
        aggregate=aggregate,
        manifest_path=str(manifest_path),
        output_hashes={
            name: entry["sha256"] for name, entry in manifest["outputs"].items()
        },
    )
