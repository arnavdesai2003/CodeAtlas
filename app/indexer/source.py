"""Read Python source with its declared encoding, without lossy replacement."""
from pathlib import Path
import tokenize


def read_python_source(path: Path) -> str:
    with tokenize.open(path) as source:
        return source.read()
