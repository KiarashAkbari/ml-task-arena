"""Smoke tests: the package imports and the CLI works offline.

These are the first entries toward the required suite (>=12 meaningful tests,
runnable with ``python -m unittest discover`` and no network).
"""

import contextlib
import io
import unittest

import data_factory
from data_factory import cli


class TestPackage(unittest.TestCase):
    def test_version_is_semver_like(self):
        self.assertRegex(data_factory.__version__, r"^\d+\.\d+\.\d+$")

    def test_version_matches_pyproject(self):
        """The installed distribution version must not drift from __version__."""
        from importlib.metadata import version

        self.assertEqual(version("data-factory"), data_factory.__version__)


class TestCli(unittest.TestCase):
    def test_default_run_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main([]), 0)

    def test_version_flag_prints_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), data_factory.__version__)


if __name__ == "__main__":
    unittest.main()
