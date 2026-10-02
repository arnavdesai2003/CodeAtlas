import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.database import Base
from app.db.models import CodeFile, Repository
from scripts.audit_python_sources import audit, inspect_source


class SourceAuditTests(unittest.TestCase):
    def test_source_categories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "valid.py").write_bytes(b"# coding: latin-1\ndef caf\xe9():\n    pass\n")
            (root / "broken.py").write_text("def broken(:\n    pass\n")
            (root / "encoding.py").write_bytes(b"\xff")
            (root / "link.py").symlink_to(root / "valid.py")
            self.assertIsNone(inspect_source(root, "valid.py"))
            self.assertEqual(inspect_source(root, "broken.py"), "grammar_recovery")
            self.assertEqual(inspect_source(root, "encoding.py"), "source_read_error")
            for path in ("link.py", "missing.py", "../valid.py"):
                self.assertEqual(inspect_source(root, path), "unavailable_or_excluded")

    def test_registered_python_only_and_no_writes(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with tempfile.TemporaryDirectory() as directory, Session(engine) as db:
            root = Path(directory)
            clone = root / "owner" / "repo"
            clone.mkdir(parents=True)
            (clone / "ok.py").write_text("def ok():\n    pass\n")
            repository = Repository(name="repo", clone_url="https://github.com/owner/repo")
            db.add(repository)
            db.flush()
            db.add_all([
                CodeFile(repository_id=repository.id, path="ok.py", language="python"),
                CodeFile(repository_id=repository.id, path="missing.py", language="python"),
                CodeFile(repository_id=repository.id, path="other.js", language="javascript"),
            ])
            db.commit()
            report = audit(db, repository.id, root)
            self.assertEqual(report["python_files"], 2)
            self.assertEqual(report["clean_files"], 1)
            self.assertEqual(report["findings"][0]["path"], "missing.py")
            self.assertFalse(db.new or db.dirty or db.deleted)
            self.assertEqual(db.query(CodeFile).count(), 3)
            with self.assertRaises(ValueError):
                audit(db, 999, root)
        engine.dispose()
