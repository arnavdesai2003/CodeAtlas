"""Independent Tree-sitter parser instances under concurrent calls."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
import unittest
from unittest.mock import Mock, patch

from app.indexer import parser


class ParserConcurrencyTests(unittest.TestCase):
    def test_concurrent_calls_have_independent_parsers_and_source_results(self):
        constructor = parser.Parser
        gate = Barrier(6)
        lock = Lock()
        instances = []

        def create(language):
            instance = constructor(language)
            with lock:
                instances.append(instance)
            gate.wait(timeout=5)
            return instance

        sources = [f'class Type{i}:\n    def run{i}(self):\n        return "value{i}"\n' for i in range(6)]
        with patch.object(parser, "Parser", side_effect=create), ThreadPoolExecutor(max_workers=6) as executor:
            outcomes = list(executor.map(parser.parse_python_source, sources))
        self.assertEqual(len({id(instance) for instance in instances}), 6)
        for i, symbols in enumerate(outcomes):
            self.assertEqual([symbol.qualified_name for symbol in symbols], [f"Type{i}", f"Type{i}.run{i}"])
            self.assertEqual(symbols[1].kind, "method")
            self.assertIn(f'"value{i}"', symbols[1].code)
            self.assertEqual(symbols[1].start_line, 2)

    def test_failed_parser_instance_is_not_reused(self):
        broken = Mock()
        broken.parse.side_effect = RuntimeError("parse failure")
        with patch.object(parser, "Parser", return_value=broken):
            with self.assertRaises(RuntimeError):
                parser.parse_python_source("def bad(): pass")
        self.assertEqual(parser.parse_python_source("def next_call(): pass")[0].name, "next_call")
