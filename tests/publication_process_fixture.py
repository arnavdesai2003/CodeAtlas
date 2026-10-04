"""Disk-backed simulated publication side effects for spawned recovery tests."""
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

from app.db.models import IndexPublicationJob, SearchIndexGeneration
from app.search import engine, publication
from recovery_support import _sessions

SOURCE = engine.INDEX_NAME + "_generation_" + "a" * 32
STAGE = engine.INDEX_NAME + "_generation_" + "b" * 32


def _publish(database, external, boundary=None, *, building=False):
    sql, sessions = _sessions(database)
    state_path = Path(external)
    def read():
        return json.loads(state_path.read_text())
    def write(state):
        state_path.write_text(json.dumps(state))
    def stop(point):
        if boundary == point:
            os._exit(74)
    client = Mock()
    client.options.return_value = client
    client.indices.get_settings.side_effect = lambda **kw: {kw["index"]: {
        "settings": {"index": {"uuid": "uuid-" + kw["index"]}}}}
    client.count.return_value = {"count": 1, "_shards": {"failed": 0}}
    client.reindex.side_effect = AssertionError("Ready stage rebuilt")
    client.indices.create.side_effect = AssertionError("Ready stage recreated")
    def write_symbols(db, repository_id, *, index_name):
        state = read()
        state["writes"].append(index_name)
        write(state)
        stop("after_symbols")
        return {"symbols_indexed": 0}
    if building:
        client.indices.get_mapping.side_effect = lambda **kw: {kw["index"]: {"mappings": {}}}
        def create(**kwargs):
            state = read()
            state["indices"][kwargs["index"]] = 0
            write(state)
            stop("after_create")
            return {"acknowledged": True, "shards_acknowledged": True}
        def copy(**kwargs):
            state = read()
            stage = kwargs["dest"]["index"]
            state["indices"][stage] = 1
            state["copies"].append(stage)
            write(state)
            stop("after_copy")
            return {"timed_out": False, "failures": [], "version_conflicts": 0,
                    "total": 1, "created": 1}
        client.indices.create.side_effect = create
        client.reindex.side_effect = copy
        client.indices.refresh.return_value = {"_shards": {"failed": 0}}
        client.count.side_effect = lambda **kw: {"count": read()["indices"][kw["index"]],
                                                "_shards": {"failed": 0}}
    def swap(**kwargs):
        stop("before_alias")
        state = read()
        state["active"] = kwargs["actions"][-1]["add"]["index"]
        state["switches"] += 1
        write(state)
        stop("after_alias")
        return {"acknowledged": True}
    client.indices.update_aliases.side_effect = swap
    def invalidate(*, strict):
        assert strict
        stop("before_cache")
        state = read()
        state["rotations"] += 1
        write(state)
        stop("after_cache")
        return 0
    try:
        with sessions() as db, \
             patch.object(engine, "elasticsearch_client", client), \
             patch.object(engine, "resolve_search_index", side_effect=lambda: read()["active"]), \
             patch.object(engine, "_write_repository_index", side_effect=(write_symbols if building else
                          AssertionError("Ready symbols rewritten"))), \
             patch.object(publication, "alias_target", side_effect=lambda *a, **k: read()["active"]), \
             patch.object(publication, "invalidate_search_cache", side_effect=invalidate):
            commit = db.commit
            commits = 0
            def interrupted_commit():
                nonlocal commits
                commits += 1
                if building:
                    job = db.get(IndexPublicationJob, 1)
                    phase = job.phase if job is not None else "final"
                    if phase == "ready":
                        stop("before_ready_commit")
                    commit()
                    if phase == "ready":
                        stop("after_ready_commit")
                    elif phase == "building":
                        row = db.get(SearchIndexGeneration, job.staging_index)
                        stop("after_identity_commit" if row.index_uuid else "after_build_journal_commit")
                    return
                stop("before_published_commit" if commits == 1 else "before_final_commit")
                commit()
                if commits == 2:
                    stop("after_final_commit")
            with patch.object(db, "commit", side_effect=interrupted_commit):
                return engine.index_repository_in_elasticsearch(db, 1)
    finally:
        sql.dispose()


