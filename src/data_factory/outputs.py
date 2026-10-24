"""Outputs stage: aggregate table, quality report, manifest (AGENTS 4.5/4.8.4).

Runs after clean; produces outputs 2 and 3 (output 1, the case dataset, is
what ``clean`` writes) plus the generated data-quality report. The manifest
cross-checks that curated rows equal gate-passing records before publishing
hashes — outputs are never reported as consistent when they are not.
"""

from __future__ import annotations

from dataclasses import dataclass

from data_factory.aggregate import AggregationResult, run_aggregate
from data_factory.config import Settings
from data_factory.manifest import collect_manifest, write_manifest
from data_factory.quality import QualityReport, run_quality_report


@dataclass(frozen=True)
class OutputsResult:
    aggregate: AggregationResult
    quality: QualityReport
    manifest_path: str
    output_hashes: dict[str, str]  # file name -> sha256 (from the manifest)


def run_outputs(
    settings: Settings, *, started_at: str, finished_at: str
) -> OutputsResult:
    """Aggregate, profile, then hash everything into the manifest.

    Order matters: the quality report is generated from the case dataset
    *before* the manifest is collected, so the manifest can hash it like any
    other output.
    """
    aggregate = run_aggregate(settings)
    quality = run_quality_report(settings)
    manifest = collect_manifest(settings, started_at=started_at, finished_at=finished_at)
    manifest_path = write_manifest(settings, manifest)
    return OutputsResult(
        aggregate=aggregate,
        quality=quality,
        manifest_path=str(manifest_path),
        output_hashes={
            name: entry["sha256"] for name, entry in manifest["outputs"].items()
        },
    )
