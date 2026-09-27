from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_team.cli import main
from ai_team.config import resolve_command


class ResolveCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bin = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def install(self, relative: str) -> Path:
        path = self.bin / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return path

    def test_falls_back_to_newest_install_location(self):
        old = self.install("old/codex.exe")
        new = self.install("new/codex.exe")
        os.utime(old, (time.time() - 100, time.time() - 100))
        locations = {"codex": [str(self.bin / "*" / "codex.exe")]}
        with patch("ai_team.config.shutil.which", return_value=None), \
             patch.dict("ai_team.config.KNOWN_LOCATIONS", locations):
            self.assertEqual(resolve_command("codex"), str(new))
            # A pinned path from a previous update resolves to the current install.
            self.assertEqual(resolve_command(str(self.bin / "gone" / "codex.exe")), str(new))
            self.assertIsNone(resolve_command("unknown-cli"))

    def test_path_lookup_wins(self):
        with patch("ai_team.config.shutil.which", return_value="/usr/bin/codex"):
            self.assertEqual(resolve_command("codex"), "/usr/bin/codex")


class UiStartupTests(unittest.TestCase):
    def test_ui_initializes_repository_from_subdirectory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
            nested = root / "src" / "pkg"
            nested.mkdir(parents=True)
            with patch("ai_team.server.run_ui") as run_ui:
                self.assertEqual(main(["--project", str(nested), "ui", "--no-browser"]), 0)
            self.assertTrue((root / ".ai-team" / "config.yaml").exists())
            self.assertFalse((nested / ".ai-team").exists())
            run_ui.assert_called_once_with(root.resolve(), 8765, False)

    def test_non_repository_reports_error(self):
        with tempfile.TemporaryDirectory() as temp, patch("ai_team.cli.report_error") as report:
            self.assertEqual(main(["--project", temp, "ui"]), 2)
        self.assertIn("not inside a Git repository", report.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
