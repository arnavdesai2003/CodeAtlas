"""Shared offline database and bounded child-process fixtures."""
import multiprocessing

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

URL = "https://github.com/owner/fixture"
SOURCE = "def ingested():\n    return 1\n"


def _sessions(path):
    engine = create_engine("sqlite:///" + str(path))
    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    return engine, sessionmaker(engine, autoflush=False, expire_on_commit=False)


def stop_child(child):
    """Stop only the supplied owned child and close its process handle."""
    if child.pid is not None:
        if child.is_alive():
            child.terminate()
        child.join(5)
        if child.is_alive():
            child.kill()
            child.join(5)
        if child.is_alive():
            raise AssertionError("Owned test child could not be stopped.")
    child.close()


def run_exit_child(target, args, *, exitcode, kwargs=None, timeout=15):
    child = multiprocessing.get_context("spawn").Process(target=target, args=args, kwargs=kwargs or {})
    try:
        child.start()
        child.join(timeout)
        if child.is_alive():
            raise AssertionError("Recovery child exceeded its exit budget.")
        if child.exitcode != exitcode:
            raise AssertionError(f"Recovery child exit was {child.exitcode}; expected {exitcode}.")
    finally:
        stop_child(child)
