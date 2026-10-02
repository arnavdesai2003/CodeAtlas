"""Stationary clone file-selection boundaries; no service access."""
from pathlib import Path
import tempfile
import unittest

from app.indexer.paths import regular_source_path, clone_directory
from app.indexer.errors import UnsafeClonePath
from app.indexer.repository import discover_source_files


class SourcePathTests(unittest.TestCase):
    def test_clone_components_reject_links_and_regular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "outside"
            target.mkdir()
            for level in (0, 1, 2):
                for kind in ("link", "dangling", "file"):
                    root = base / f"root-{level}-{kind}"
                    candidate = (root, root / "owner", root / "owner/repo")[level]
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    if kind == "file":
                        candidate.write_text("occupied")
                    else:
                        candidate.symlink_to(target if kind == "link" else base / "missing")
                    with self.subTest(level=level, kind=kind), self.assertRaises(UnsafeClonePath):
                        clone_directory(root, "owner", "repo")
            self.assertEqual(clone_directory(base / "new", "owner", "repo"), base / "new/owner/repo")

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
