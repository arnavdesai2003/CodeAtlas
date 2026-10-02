"""Stationary clone file-selection boundaries; no service access."""
from pathlib import Path
import tempfile
import unittest

from app.indexer.paths import regular_source_path
from app.indexer.repository import discover_source_files


class SourcePathTests(unittest.TestCase):
    def test_discovery_excludes_file_directory_and_dangling_links(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "clone"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (outside / "secret.py").write_text("secret")
            (root / "safe.py").write_text("def safe(): pass")
            (root / "external.py").symlink_to(outside / "secret.py")
            (root / "internal.py").symlink_to(root / "safe.py")
            (root / "missing.py").symlink_to(base / "absent")
            (root / "linked").symlink_to(outside, target_is_directory=True)
            self.assertEqual([row["path"] for row in discover_source_files(root)], ["safe.py"])
            for name in ("external.py", "internal.py", "missing.py", "linked/secret.py",
                         "../outside/secret.py", str(outside / "secret.py"), ".git/config", ""):
                with self.subTest(name=name):
                    self.assertIsNone(regular_source_path(root, name))
            self.assertEqual(regular_source_path(root, "safe.py"), root / "safe.py")
