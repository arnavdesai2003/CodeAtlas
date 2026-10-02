from dataclasses import dataclass

import tree_sitter_python
from tree_sitter import Language, Parser


PYTHON_LANGUAGE = Language(tree_sitter_python.language())



@dataclass
class CodeSymbol:
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    code: str


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode(
        "utf-8",
        errors="replace",
    )


def _symbol_name(node, source: bytes) -> str:
    name_node = node.child_by_field_name("name")

    if name_node is None:
        return "<anonymous>"

    return _node_text(name_node, source)


def parse_python_source(source_code: str) -> list[CodeSymbol]:
    source = source_code.encode("utf-8")

    tree = Parser(PYTHON_LANGUAGE).parse(source)

    symbols: list[CodeSymbol] = []

    # Carry scope explicitly so deeply nested expression trees do not consume
    # Python call-stack frames. Reverse pushes preserve the original preorder.
    pending = [(tree.root_node, None, False)]
    while pending:
        node, current_class, inside_function = pending.pop()
        child_class = current_class
        child_inside_function = inside_function
        if node.type == "class_definition":
            class_name = _symbol_name(node, source)
            symbols.append(CodeSymbol(
                name=class_name, qualified_name=class_name, kind="class",
                start_line=node.start_point.row + 1,
                end_line=node.end_point.row + 1,
                code=_node_text(node, source),
            ))
            child_class = class_name
            child_inside_function = False
        elif node.type == "function_definition":
            function_name = _symbol_name(node, source)
            if current_class is not None and not inside_function:
                kind = "method"
                qualified_name = f"{current_class}.{function_name}"
            else:
                kind = "function"
                qualified_name = function_name
            symbols.append(CodeSymbol(
                name=function_name, qualified_name=qualified_name, kind=kind,
                start_line=node.start_point.row + 1,
                end_line=node.end_point.row + 1,
                code=_node_text(node, source),
            ))
            child_inside_function = True
        pending.extend(
            (child, child_class, child_inside_function)
            for child in reversed(node.children)
        )

    return symbols
