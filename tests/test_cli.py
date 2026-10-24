"""Tests for CLI exit-code mapping (AGENTS.md section 4.1)."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from data_factory import cli
from data_factory.errors import EXIT_OK, EXIT_UNEXPECTED


class CliExitCodeTests(unittest.TestCase):
    def test_missing_config_exits_with_config_code(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cli.main(["--config", "does-not-exist.json"])
        self.assertEqual(code, 3)
        self.assertIn("cannot read config file", stderr.getvalue())

    def test_usage_error_exits_with_reserved_code_two(self) -> None:
        """argparse owns code 2 for unknown arguments; main must not swallow it."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            cli.main(["--definitely-not-a-flag"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_version_flag_exits_zero(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--version"]), EXIT_OK)

    def test_show_config_prints_resolved_settings(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["--show-config"])
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["settings"]["start_date"], "2023-01-01")
        self.assertEqual(payload["settings"]["raw_dir"], "data/raw")

    def test_unexpected_bug_exits_with_code_one(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            cli, "load_config", side_effect=RuntimeError("boom")
        ), contextlib.redirect_stderr(stderr):
            code = cli.main(["--config", "any.json"])
        self.assertEqual(code, EXIT_UNEXPECTED)
        self.assertIn("boom", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
