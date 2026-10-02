"""Traversal depth and established scope/order behavior without services."""
import unittest

from app.indexer.parser import parse_python_source


class ParserTraversalTests(unittest.TestCase):
    def test_deep_expression_does_not_hide_following_symbol(self):
        source = "value = " + "+".join(["1"] * 1500) + "\ndef after():\n    return value\n"
        compile(source, "fixture.py", "exec")
        symbols = parse_python_source(source)
        self.assertEqual(len(symbols), 1)
        symbol = symbols[0]
        self.assertEqual((symbol.name, symbol.kind, symbol.start_line, symbol.end_line),
                         ("after", "function", 2, 3))
        self.assertEqual(symbol.code, "def after():\n    return value")

    def test_scope_and_preorder_preserved_across_siblings(self):
        source = (
            "class Outer:\n"
            "    def first(self):\n"
            "        def local():\n"
            "            pass\n"
            "    class Inner:\n"
            "        async def run(self):\n"
            "            pass\n"
            "    def second(self):\n"
            "        pass\n"
            "def outside():\n"
            "    pass\n"
        )
        symbols = parse_python_source(source)
        self.assertEqual([(s.qualified_name, s.kind) for s in symbols], [
            ("Outer", "class"), ("Outer.first", "method"),
            ("local", "function"), ("Inner", "class"),
            ("Inner.run", "method"), ("Outer.second", "method"),
            ("outside", "function"),
        ])
        self.assertEqual([(s.start_line, s.end_line) for s in symbols],
                         [(1, 9), (2, 4), (3, 4), (5, 7), (6, 7), (8, 9), (10, 11)])
