"""Ground truth must identify one repository before scoring name-based hits."""
import unittest
from unittest.mock import patch
from app.db.database import Base
from app.db.models import Repository, CodeFile, CodeSymbol
from recovery_support import _sessions
from scripts import evaluate_multirepo as command


class EvaluationGroundTruthTests(unittest.TestCase):
    def setUp(self):
        self.sql, self.sessions = _sessions(":memory:")
        self.addCleanup(self.sql.dispose)
        Base.metadata.create_all(self.sql)
        with self.sessions() as db:
            for identifier, name in ((1, "shared"), (2, "shared"), (3, "unique")):
                db.add(Repository(id=identifier, name=name, clone_url=f"https://github.com/owner{identifier}/{name}"))
                db.flush()
                db.add(CodeFile(id=identifier, repository_id=identifier, path="main.py", language="python"))
                db.flush()
                db.add(CodeSymbol(repository_id=identifier, file_id=identifier, name="target",
                    qualified_name="target", kind="function", start_line=1, end_line=1, code="def target(): pass"))
            db.commit()

    def test_duplicate_repository_names_are_invalid_despite_matching_symbols(self):
        case = {"repository": "shared", "query": "example", "expected": "target"}
        with patch.object(command, "SessionLocal", self.sessions), patch.object(command, "TEST_CASES", [case]):
            valid, invalid = command.validate_test_cases()
        self.assertEqual(valid, [])
        self.assertEqual(invalid, [{**case, "reason": "repository name is ambiguous"}])

    def test_explicit_case_input_does_not_use_default_cases(self):
        case = {"repository": "unique", "query": "example", "expected": "target"}
        with patch.object(command, "SessionLocal", self.sessions), \
             patch.object(command, "TEST_CASES", [{**case, "repository": "missing"}]):
            self.assertEqual(command.validate_test_cases([case]), ([case], []))
            self.assertEqual(command.validate_test_cases([]), ([], []))

    def test_unique_and_missing_ground_truth_keep_existing_classification(self):
        cases = [{"repository": "unique", "query": "example", "expected": "target"},
                 {"repository": "missing", "query": "example", "expected": "target"},
                 {"repository": "unique", "query": "example", "expected": "absent"}]
        with patch.object(command, "SessionLocal", self.sessions), patch.object(command, "TEST_CASES", cases):
            valid, invalid = command.validate_test_cases()
        self.assertEqual(valid, cases[:1])
        self.assertEqual([row["reason"] for row in invalid], ["repository not found", "symbol not found"])
