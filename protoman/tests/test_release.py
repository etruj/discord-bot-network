#!/usr/bin/env python3
"""Guards the public repository against committed secrets."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "data"}
TEXT = {".py", ".md", ".txt", ".yml", ".scad", ".sh", ".service", ".example", ""}
PATTERNS = {
    "Discord bot token": r"[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}",
    "Claude session link": r"claude\.ai/code\?environment=env_",
    "absolute home path": r"/home/[a-z][a-z0-9_-]+/",
}


def files():
    for p in ROOT.rglob("*"):
        if p.is_file() and not SKIP_DIRS & set(p.relative_to(ROOT).parts) and p.suffix in TEXT:
            yield p


class TestNoSecrets(unittest.TestCase):
    def test_no_tokens_or_private_paths(self):
        found = []
        for p in files():
            text = p.read_text(errors="ignore")
            found += [f"{p.relative_to(ROOT)}: {what}"
                      for what, rx in PATTERNS.items() if re.search(rx, text)]
        self.assertEqual(found, [])

    def test_secret_files_are_ignored(self):
        if not (ROOT / ".gitignore").exists():
            self.skipTest("not a repository checkout (e.g. the Docker image)")
        ignored = (ROOT / ".gitignore").read_text().split()
        self.assertIn(".env", ignored)
        self.assertIn(".protoman.json", ignored)


if __name__ == "__main__":
    unittest.main()
