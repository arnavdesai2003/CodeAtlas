"""Argument validation for single-repository indexing commands."""
import argparse
from app.indexer.locking import validate_repository_id


def repository_id_argument(value):
    try:
        repository_id = int(value)
        validate_repository_id(repository_id)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("repository ID must be an integer from 1 to 2147483647") from None
    return repository_id


def run_indexing_command(operation, *args):
    try:
        operation(*args)
    except Exception as exc:
        print(f"{type(exc).__name__}: Indexing failed; inspect pending recovery work before retrying.")
        return 1
    return 0
