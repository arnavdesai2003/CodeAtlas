"""Verify PostgreSQL coordination across spawned processes and owner death."""
import argparse
import json
import multiprocessing
import secrets
import time

from app.db.database import SessionLocal
from app.indexer.locking import (RepositorySyncInProgress, generation_maintenance_lock,
                                 repository_sync_lock)
from scripts.verify_writer_locks import _blocked, _preflight


def _owner(pipe, repository_id, publication):
    try:
        with SessionLocal() as db:
            with repository_sync_lock(db, repository_id, publication=publication):
                pipe.send({"status": "held"})
                if pipe.recv() != "release":
                    raise RuntimeError("Unexpected owner command.")
            pipe.send({"status": "released"})
    except Exception as exc:
        pipe.send({"status": "failed", "error": type(exc).__name__})
    finally:
        pipe.close()


def _message(pipe, expected, timeout=10):
    if not pipe.poll(timeout) or pipe.recv() != {"status": expected}:
        raise RuntimeError("Owner process did not acknowledge expected state.")


def _stop(process):
    if process.is_alive():
        process.terminate()
    process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)
    if process.is_alive():
        raise RuntimeError("Probe owner could not be stopped.")


def _await_release(db, repository_id, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        try:
            with generation_maintenance_lock(db):
                pass
            with repository_sync_lock(db, repository_id):
                pass
            return
        except RepositorySyncInProgress:
            if time.monotonic() >= deadline:
                raise RuntimeError("Owner locks remained after process exit.") from None
            time.sleep(.05)


def verify_writer_processes():
    ids = (1_000_000_000 + secrets.randbelow(500_000_000),
           1_500_000_000 + secrets.randbelow(500_000_000))
    ctx = multiprocessing.get_context("spawn")
    checks = []
    with SessionLocal() as db:
        _preflight(db, ids)
        with generation_maintenance_lock(db):
            _preflight(db, ids)
        for publication in (False, True):
            for abrupt in (False, True):
                parent, child = ctx.Pipe()
                process = ctx.Process(target=_owner, args=(child, ids[0], publication))
                try:
                    process.start()
                    child.close()
                    _message(parent, "held")
                    _blocked(repository_sync_lock(db, ids[0]))
                    _blocked(generation_maintenance_lock(db))
                    if publication:
                        _blocked(repository_sync_lock(db, ids[1]))
                    else:
                        with repository_sync_lock(db, ids[1]):
                            pass
                    if abrupt:
                        _stop(process)
                    else:
                        parent.send("release")
                        _message(parent, "released")
                        process.join(5)
                        if process.is_alive() or process.exitcode != 0:
                            raise RuntimeError("Owner process did not exit cleanly.")
                    _await_release(db, ids[0])
                    checks.append(("publisher" if publication else "writer") +
                                  ("_terminated_release" if abrupt else "_normal_release"))
                finally:
                    try:
                        if process.pid is not None:
                            _stop(process)
                    finally:
                        parent.close()
                        child.close()
    return {"status": "passed", "checks": checks, "metadata_writes": False,
            "owner_processes_stopped": True}


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        report = verify_writer_processes()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__,
                          "detail": "Cross-process verification failed; no success claimed."}))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
