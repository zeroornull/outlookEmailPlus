"""Release version alignment gate (scripts/check_release_version.py)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_release_version as gate


class ReleaseVersionGateTests(unittest.TestCase):
    def test_skip_on_main_ref(self):
        self.assertEqual(gate.run_checks("refs/heads/main"), [])

    def test_ok_when_version_and_changelog_match(self):
        ref = "refs/tags/v2.7.0"
        with (
            patch.object(gate, "check_app_version", return_value=[]),
            patch.object(gate, "check_project_version", return_value=[]),
            patch.object(gate, "check_changelog", return_value=[]),
        ):
            self.assertEqual(gate.run_checks(ref), [])

    def test_errors_when_version_mismatch(self):
        ref = "refs/tags/v2.7.0"
        with (
            patch.object(gate, "check_app_version", return_value=["version mismatch"]),
            patch.object(gate, "check_project_version", return_value=[]),
            patch.object(gate, "check_changelog", return_value=[]),
        ):
            errors = gate.run_checks(ref)
        self.assertEqual(len(errors), 1)
        self.assertIn("version", errors[0])

    def test_app_version_is_read_without_importing_application(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "outlook_web"
            app_dir.mkdir()
            (app_dir / "__init__.py").write_text(
                '__version__ = "2.7.0"\nraise RuntimeError("must not import app")\n', encoding="utf-8"
            )
            with patch.object(gate, "_REPO_ROOT", root):
                self.assertEqual(gate.check_app_version("2.7.0"), [])
                errors = gate.check_app_version("2.8.0")
        self.assertEqual(len(errors), 1)
        self.assertIn("outlook_web.__version__ is '2.7.0', expected '2.8.0'", errors[0])

    def test_errors_when_project_version_mismatches_tag(self):
        with (
            patch.object(gate, "check_app_version", return_value=[]),
            patch.object(gate, "check_project_version", return_value=["project version mismatch"]),
            patch.object(gate, "check_changelog", return_value=[]),
        ):
            self.assertEqual(gate.run_checks("refs/tags/v2.7.0"), ["project version mismatch"])

    def test_project_version_matches_tag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "pyproject.toml").write_text('[project]\nversion = "2.7.0"\n', encoding="utf-8")
            with patch.object(gate, "_REPO_ROOT", root):
                self.assertEqual(gate.check_project_version("2.7.0"), [])
                errors = gate.check_project_version("2.8.0")
        self.assertEqual(len(errors), 1)
        self.assertIn("project.version is '2.7.0', expected '2.8.0'", errors[0])

    def test_project_version_is_required(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            with patch.object(gate, "_REPO_ROOT", root):
                errors = gate.check_project_version("2.7.0")
        self.assertEqual(len(errors), 1)
        self.assertIn("project.version is None", errors[0])

    def test_main_exits_zero(self):
        with patch.dict(os.environ, {"GITHUB_REF": "refs/heads/main"}, clear=False):
            self.assertEqual(gate.main(), 0)

    def test_tag_mismatch_exits_nonzero(self):
        with patch.dict(os.environ, {"GITHUB_REF": "refs/tags/v9.9.9"}, clear=False):
            code = gate.main()
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
