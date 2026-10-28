from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import Settings


class SettingsPathsTest(unittest.TestCase):
    def test_backend_working_directory_resolves_repository_root(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with patch("orchestrator.config.Path.cwd", return_value=repo_root / "backend"):
            settings = Settings(_env_file=None)

        self.assertEqual(settings.repo_root, repo_root)

    def test_web_build_path_is_at_repository_root(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        settings = Settings(_env_file=None, repo_root=repo_root)

        self.assertEqual(settings.ui_dir, repo_root / "web" / "dist")


if __name__ == "__main__":
    unittest.main()
