"""End-to-end outputs tests: wide aggregate, manifest, determinism (4.5)."""

from __future__ import annotations

import csv
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

import data_factory
from data_factory.aggregate import AGGREGATE_NAME, run_aggregate
from data_factory.clean import CASES_NAME, run_clean
from data_factory.config import Settings, load_config
from data_factory.curate import QUARANTINE_NAME, run_curate
from data_factory.errors import ValidationError
from data_factory.fetch import run_fetch
from data_factory.manifest import (
    MANIFEST_NAME,
    collect_manifest,
    sha256_file,
    write_manifest,
)
from data_factory.outputs import run_outputs
from data_factory.quality import DATA_QUALITY_NAME, run_quality_report
from tests.test_curate import ScriptedClient, healthy_record

POPULATION_CSV = (
    "zip,population,borough\n"
    "11101,30000,Queens\n"
    "11201,20000,Brooklyn\n"
)


def record(
    index: int,
    *,
    borough: str = "Queens",
    zip_code: str = "11101",
    created: str = "2023-01-15T10:00:00.000",
    closed: str | None = None,
    complaint_type: str = "Noise - Residential",
) -> dict:
    base = healthy_record(index)
    base.update(
        {
            "borough": borough,
            "incident_zip": zip_code,
            "created_date": created,
            "complaint_type": complaint_type,
        }
    )
    if closed is not None:
        base["closed_date"] = closed
    return base


class OutputsTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.population_path = self.tmp_path / "pop.csv"
        self.population_path.write_text(POPULATION_CSV, encoding="utf-8")

    def settings(self, *, batch_size: int = 10, threshold: float = 1.0) -> Settings:
        return dataclasses.replace(
            load_config(),
            batch_size=batch_size,
            raw_dir=self.tmp_path / "raw",
            curated_dir=self.tmp_path / "curated",
            quarantine_max_ratio=threshold,
            population_path=self.population_path,
        )

    def pipeline(self, records: list[dict], settings: Settings) -> ScriptedClient:
        client = ScriptedClient(records)
        run_fetch(settings, client)
        run_curate(settings)
        run_clean(settings)
        return client

    @staticmethod
    def read_csv(path: Path) -> tuple[list[str], list[dict]]:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            return list(reader.fieldnames or []), list(reader)


class AggregateTests(OutputsTestBase):
    def _fixture_records(self) -> list[dict]:
        return [
            record(0, created="2023-01-15T10:00:00.000"),
            record(1, created="2023-02-10T10:00:00.000"),
            record(2, borough="Brooklyn", zip_code="11201", created="2023-01-20T10:00:00.000"),
            record(3, borough="Unspecified", zip_code="N/A", created="2023-01-05T10:00:00.000"),
            record(4, created="2023-01-25T10:00:00.000", complaint_type="Noise - Park"),
        ]

    def test_wide_grid_counts_and_population_rates(self) -> None:
        settings = self.settings()
        self.pipeline(self._fixture_records(), settings)
        result = run_aggregate(settings)

        self.assertEqual(result.months, ("2023-01", "2023-02"))
        self.assertEqual(result.districts, ("Brooklyn", "Queens", "Unspecified"))
        self.assertEqual(result.rows_written, 3 * 6)  # full district x locked-type grid

        header, rows = self.read_csv(Path(settings.curated_dir) / AGGREGATE_NAME)
        self.assertEqual(
            header,
            ["borough", "complaint_type", "2023-01", "2023-02", "total",
             "rate_2023-01", "rate_2023-02", "total_rate"],
        )
        by_key = {(r["borough"], r["complaint_type"]): r for r in rows}

        queens_res = by_key[("Queens", "Noise - Residential")]
        self.assertEqual(queens_res["2023-01"], "1")
        self.assertEqual(queens_res["2023-02"], "1")
        self.assertEqual(queens_res["total"], "2")
        # 1 / 30,000 * 100,000 = 3.33 ; 2 / 30,000 * 100,000 = 6.67
        self.assertEqual(queens_res["rate_2023-01"], "3.33")
        self.assertEqual(queens_res["total_rate"], "6.67")

        brooklyn_res = by_key[("Brooklyn", "Noise - Residential")]
        self.assertEqual(brooklyn_res["2023-01"], "1")
        self.assertEqual(brooklyn_res["2023-02"], "0")
        self.assertEqual(brooklyn_res["rate_2023-01"], "5.00")
        self.assertEqual(brooklyn_res["rate_2023-02"], "0.00")

        # Zero-filled grid rows exist with zero counts/rates.
        brooklyn_park = by_key[("Brooklyn", "Noise - Park")]
        self.assertEqual(brooklyn_park["total"], "0")
        self.assertEqual(brooklyn_park["rate_2023-01"], "0.00")

        # District without a reference population: blank rates, never fabricated.
        unspec = by_key[("Unspecified", "Noise - Residential")]
        self.assertEqual(unspec["2023-01"], "1")
        self.assertEqual(unspec["rate_2023-01"], "")
        self.assertEqual(unspec["total_rate"], "")

    def test_aggregate_requires_clean_output(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            run_aggregate(self.settings())
        self.assertIn("clean stage", str(ctx.exception))

    def test_aggregate_requires_population_with_borough(self) -> None:
        settings = self.settings()
        self.pipeline([record(0)], settings)  # built against the good reference
        self.population_path.write_text(
            "zip,population\n11101,30000\n", encoding="utf-8"
        )
        with self.assertRaises(ValidationError) as ctx:
            run_aggregate(settings)
        self.assertIn("borough", str(ctx.exception))


class ManifestTests(OutputsTestBase):
    def test_manifest_fields_and_output_hashes(self) -> None:
        settings = self.settings()
        client = self.pipeline([record(i) for i in range(5)], settings)
        result = run_outputs(
            settings,
            started_at="2026-10-24T10:00:00+00:00",
            finished_at="2026-10-24T10:00:05+00:00",
        )
        manifest = json.loads(
            (Path(settings.curated_dir) / MANIFEST_NAME).read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["code_version"], data_factory.__version__)
        self.assertEqual(
            manifest["window"],
            {"start": "2023-01-01", "end": "2024-06-30", "mode": "configured"},
        )
        self.assertEqual(manifest["archived_windows"], [])
        self.assertEqual(manifest["scope"]["agency"], "NYPD")
        self.assertEqual(len(manifest["scope"]["complaint_types"]), 6)
        self.assertEqual(manifest["fetch"]["records_raw"], 5)
        self.assertEqual(manifest["fetch"]["http_requests"], client.request_count)
        self.assertEqual(manifest["fetch"]["peak_in_memory_records"], 5)
        self.assertEqual(manifest["passed_records"], 5)
        self.assertEqual(manifest["quarantine"]["records"], 0)
        self.assertEqual(manifest["quarantine"]["by_reason"], {})
        self.assertEqual(manifest["started_at"], "2026-10-24T10:00:00+00:00")
        self.assertEqual(manifest["finished_at"], "2026-10-24T10:00:05+00:00")
        self.assertEqual(
            manifest["population_source"]["path"],
            settings.population_path.as_posix(),
        )
        # The manifest mirrors whatever the config records: empty until the
        # student picks a source, then the real URL and fetch date. Asserting a
        # fixed value here would break the moment the committed config is
        # filled in, which is the state the delivery ships in.
        self.assertEqual(
            manifest["population_source"]["source_url"],
            settings.population_source_url or None,
        )
        self.assertEqual(
            manifest["population_source"]["fetched_date"],
            settings.population_fetched_date.isoformat()
            if settings.population_fetched_date
            else None,
        )

        curated = Path(settings.curated_dir)
        self.assertEqual(
            sorted(manifest["outputs"]),
            sorted([AGGREGATE_NAME, CASES_NAME, QUARANTINE_NAME, DATA_QUALITY_NAME]),
        )
        for name, entry in manifest["outputs"].items():
            with self.subTest(output=name):
                self.assertEqual(entry["sha256"], sha256_file(curated / name))
                self.assertEqual(entry["bytes"], (curated / name).stat().st_size)
        self.assertEqual(sorted(result.output_hashes), sorted(manifest["outputs"]))

    def test_manifest_records_an_unfilled_population_source_as_null(self) -> None:
        """Before a source is chosen the manifest must say null, not invent one."""
        settings = dataclasses.replace(
            self.settings(), population_source_url="", population_fetched_date=None
        )
        self.pipeline([record(0)], settings)
        run_outputs(settings, started_at="s", finished_at="f")
        manifest = json.loads(
            (Path(settings.curated_dir) / MANIFEST_NAME).read_text(encoding="utf-8")
        )
        self.assertIsNone(manifest["population_source"]["source_url"])
        self.assertIsNone(manifest["population_source"]["fetched_date"])

    def test_manifest_breaks_down_quarantine_reasons(self) -> None:
        settings = self.settings()
        records = [record(i) for i in range(4)] + [
            {**record(9), "incident_zip": "00000"},
        ]
        self.pipeline(records, settings)
        run_outputs(settings, started_at="s", finished_at="f")
        manifest = json.loads(
            (Path(settings.curated_dir) / MANIFEST_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["fetch"]["records_raw"], 5)
        self.assertEqual(manifest["quarantine"]["records"], 1)
        self.assertEqual(manifest["quarantine"]["by_reason"], {"INVALID_ZIP_CODE": 1})
        self.assertEqual(manifest["passed_records"], 4)

    def test_manifest_cross_check_detects_out_of_sync_outputs(self) -> None:
        settings = self.settings()
        self.pipeline([record(i) for i in range(5)], settings)
        run_aggregate(settings)
        run_quality_report(settings)
        cases_path = Path(settings.curated_dir) / CASES_NAME
        lines = cases_path.read_text(encoding="utf-8").splitlines()
        cases_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        with self.assertRaises(ValidationError) as ctx:
            collect_manifest(settings, started_at="s", finished_at="f")
        self.assertIn("cross-check failed", str(ctx.exception))

    def test_manifest_requires_aggregate_output(self) -> None:
        settings = self.settings()
        self.pipeline([record(0)], settings)
        with self.assertRaises(ValidationError) as ctx:
            collect_manifest(settings, started_at="s", finished_at="f")
        self.assertIn("aggregate", str(ctx.exception))


class DeterminismTests(OutputsTestBase):
    def test_two_independent_runs_produce_identical_output_hashes(self) -> None:
        """4.5: two independent runs on the same window -> same output hashes."""
        records = [
            record(0, created="2023-01-15T10:00:00.000"),
            record(1, created="2023-02-10T10:00:00.000"),
            record(2, borough="Brooklyn", zip_code="11201"),
            record(3, complaint_type="Noise - Park"),
            {**record(4), "incident_zip": "00000"},  # quarantined
        ]

        manifests = []
        byte_sets = []
        for run in ("a", "b"):
            settings = dataclasses.replace(
                self.settings(batch_size=3),
                raw_dir=self.tmp_path / f"raw_{run}",
                curated_dir=self.tmp_path / f"curated_{run}",
            )
            self.pipeline(records, settings)
            run_outputs(settings, started_at=f"start-{run}", finished_at=f"end-{run}")
            curated = Path(settings.curated_dir)
            manifests.append(
                json.loads((curated / MANIFEST_NAME).read_text(encoding="utf-8"))
            )
            byte_sets.append(
                {
                    name: (curated / name).read_bytes()
                    for name in (
                        CASES_NAME,
                        AGGREGATE_NAME,
                        QUARANTINE_NAME,
                        DATA_QUALITY_NAME,
                    )
                }
            )

        # Data outputs byte-identical...
        self.assertEqual(byte_sets[0], byte_sets[1])
        # ...manifest hashes identical (manifest bytes differ only by timestamps).
        self.assertEqual(manifests[0]["outputs"], manifests[1]["outputs"])
        self.assertNotEqual(manifests[0]["started_at"], manifests[1]["started_at"])

    def test_manifest_write_is_atomic_and_parses(self) -> None:
        settings = self.settings()
        self.pipeline([record(0)], settings)
        run_aggregate(settings)
        run_quality_report(settings)
        manifest = collect_manifest(settings, started_at="s", finished_at="f")
        path = write_manifest(settings, manifest)
        self.assertTrue(path.is_file())
        self.assertEqual(list(path.parent.glob("*.tmp")), [])
        json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
