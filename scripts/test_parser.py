from pathlib import Path

from app.indexer.parser import parse_python_source


repo_root = Path("data/repos/karpathy/micrograd")

python_files = list(repo_root.rglob("*.py"))

print(f"Python files found: {len(python_files)}")
print()


for path in python_files:
    source = path.read_text(
        encoding="utf-8",
        errors="replace",
    )

    symbols = parse_python_source(source)

    if not symbols:
        continue

    print("=" * 70)
    print(path)
    print("=" * 70)

    for symbol in symbols:
        print(
            f"{symbol.kind:<10} "
            f"{symbol.qualified_name:<30} "
            f"lines {symbol.start_line}-{symbol.end_line}"
        )