"""Encoding fixtures; no services or model downloads."""
from pathlib import Path
import tempfile
import unittest

from app.indexer.source import read_python_source
from app.indexer.parser import parse_python_source


class SourceEncodingTests(unittest.TestCase):
    def test_declared_encoding_preserves_symbol_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latin.py"
            path.write_bytes(b'# coding: latin-1\ndef greeting():\n    return "caf\xe9"\n')
            symbols = parse_python_source(read_python_source(path))
            self.assertEqual(symbols[0].name, "greeting")
            self.assertIn("café", symbols[0].code)
            self.assertNotIn("\ufffd", symbols[0].code)

    def test_bom_and_second_line_cookie(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.py"
            path.write_bytes(b"\xef\xbb\xbfdef run(): pass\n")
            self.assertEqual(read_python_source(path), "def run(): pass\n")
            path.write_bytes(b'#!/usr/bin/python\n# coding: latin-1\nvalue = "\xe9"\n')
            self.assertIn('"é"', read_python_source(path))

    def test_invalid_bytes_or_encoding_declaration_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.py"
            for content in (b'def run(): return "\xff"\n', b"# coding: unknown-encoding\n",
                            b"\xef\xbb\xbf# coding: latin-1\n"):
                path.write_bytes(content)
                with self.subTest(content=content), self.assertRaises((SyntaxError, UnicodeError)):
                    read_python_source(path)
