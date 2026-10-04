"""Exercise publication against scratch ES/Redis artifacts and SQLite metadata."""
import io
import argparse
import json
from contextlib import redirect_stdout
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, delete, event
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import (Repository, CodeFile, CodeSymbol, IndexPublicationJob,
                           RepositoryFullIndexJob, SearchIndexGeneration)
from app.search import cache, engine, publication
from app.search.generation_lifecycle import index_uuid
from app.search.generation_retention import _assert_idle
from app.search.indexes import alias_target, resolve_active_index
from app.search.responses import response_body
from scripts.verify_cache_protocol import _clear_namespace, _require

VECTOR = [1.0] + [0.0] * 383


class ObservedClient:
    """Delegate real operations while injecting one lost alias acknowledgement."""
    def __init__(self, client, state):
        self.client = client
        self.state = state

    def __getattr__(self, name):
        return getattr(self.client, name)

    def options(self, **kwargs):
        return ObservedClient(self.client.options(**kwargs), self.state)

    @property
    def indices(self):
        outer = self

        class Indices:
            def __getattr__(self, name):
                return getattr(outer.client.indices, name)

            def update_aliases(self, **kwargs):
                response = outer.client.indices.update_aliases(**kwargs)
                outer.state["switches"] += 1
                if outer.state.pop("lose_ack", False):
                    raise TimeoutError("Injected lost alias acknowledgement.")
                return response

        return Indices()

    def reindex(self, **kwargs):
        self.state["copies"] += 1
        return self.client.reindex(**kwargs)


def _normal_snapshot(client, legacy, alias):
    active = resolve_active_index(client, legacy_name=legacy, alias_name=alias)
    return {"active": active, "uuid": index_uuid(client, active),
            "count": publication.checked_count(client, index=active)}


def _clear_indices(client, base, identities):
    metadata = response_body(client.indices.get(
        index=f"{base}*", allow_no_indices=True, ignore_unavailable=True,
        expand_wildcards="all", filter_path="*.aliases,*.settings.index.uuid",
    ))
    _require(isinstance(metadata, dict), "Scratch inventory was malformed.")
    # Validate the entire observed set before issuing any exact-name deletion.
    for name in metadata:
        _require(name == base or name.startswith(base + "_generation_"), "Unexpected scratch index name.")
        entry = metadata[name]
        aliases = entry.get("aliases", {}) if isinstance(entry, dict) else None
        _require(isinstance(aliases, dict) and set(aliases) <= {base + "_active"},
                 "Scratch index acquired an unexpected alias.")
        _require(name in identities and index_uuid(client, name) == identities[name], "Scratch identity was unverified.")
    _assert_idle(client)
    for name in sorted(metadata):
        _assert_idle(client)
        _require(index_uuid(client, name) == identities[name], "Scratch identity changed before cleanup.")
        response = response_body(client.indices.delete(index=name))
        _require(isinstance(response, dict) and response.get("acknowledged") is True, "Scratch deletion was ambiguous.")
    remaining = response_body(client.indices.get(
        index=f"{base}*", allow_no_indices=True, ignore_unavailable=True, expand_wildcards="all",
    ))
    _require(remaining == {}, "Scratch index cleanup was incomplete.")


def verify_publication_protocol(es, redis, *, on_namespace=None):
    token = uuid4().hex
    base = f"codeatlas_verification_publication_{token}"
    alias = base + "_active"
    prefix = f"codeatlas:verification:publication:{token}:"
    if on_namespace:
        on_namespace({"index_prefix": base, "alias": alias, "redis_prefix": prefix})
    normal_legacy, normal_alias = engine.INDEX_NAME, engine.SEARCH_ALIAS
    normal = _normal_snapshot(es, normal_legacy, normal_alias)
    normal_generation_key = cache.GENERATION_KEY
    normal_generation = redis.get(normal_generation_key)
    _require(not es.indices.exists(index=base), "Scratch source already exists.")
    sql = create_engine("sqlite://")
    @event.listens_for(sql, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(sql)
    db = sessionmaker(sql, expire_on_commit=False, autoflush=False)()
    identities = {}
    checks = []
    state = {"copies": 0, "switches": 0}
    observed = ObservedClient(es, state)
    provision = engine.create_symbol_index
    try:
        with patch.object(engine, "INDEX_NAME", base), patch.object(engine, "SEARCH_ALIAS", alias), \
             patch.object(engine, "elasticsearch_client", observed), \
             patch.object(engine, "create_symbol_index", side_effect=lambda client=observed, **kw: provision(client, **kw)), \
             patch.object(engine, "embed_texts", side_effect=lambda texts, **kw: [VECTOR.copy() for _ in texts]), \
             patch.object(engine, "embed_text", return_value=VECTOR.copy()), \
             patch.object(cache, "redis_client", redis), patch.object(cache, "GENERATION_KEY", prefix + "generation"), \
             patch.object(cache, "ENTRY_PREFIX", prefix + "entries:"), redirect_stdout(io.StringIO()):
            repos = [Repository(name=name, clone_url=f"https://github.com/verification/{name}",
                                last_indexed_commit="unchanged") for name in ("target", "other")]
            db.add_all(repos)
            db.flush()
            files = [CodeFile(repository_id=repo.id, path=path, language="python")
                     for repo, path in zip(repos, ("main.py", "helper.py"))]
            db.add_all(files)
            db.flush()
            symbols = [CodeSymbol(repository_id=repo.id, file_id=file.id, name=repo.name,
                qualified_name=repo.name, kind="function", start_line=1, end_line=2,
                code=f"def {repo.name}():\n    return 1") for repo, file in zip(repos, files)]
            db.add_all(symbols)
            db.commit()
            engine.create_symbol_index(es, index_name=base)
            identities[base] = index_uuid(es, base)
            for repo in repos:
                engine._write_repository_index(db, repo.id, index_name=base)
            es.index(index=base, id="obsolete", refresh=True, document={
                "repository_id": repos[0].id, "repository": "target", "file_id": files[0].id,
                "symbol_id": 999999, "path": "obsolete.py", "language": "python", "is_test": False,
                "name": "obsolete", "qualified_name": "obsolete", "kind": "function",
                "start_line": 1, "end_line": 2, "code": "def obsolete():\n    pass", "embedding": VECTOR,
            })
            _require(publication.checked_count(es, index=base) == 3, "Scratch seeding failed.")
            old = cache.get_cached_search("verification", 10)
            _require(cache.set_cached_search("verification", 10, [{"name": "old"}], generation=old.generation),
                     "Private cache seeding failed.")
            first = engine.index_repository_in_elasticsearch(db, repos[0].id)
            _require(first["documents_copied"] == 1 and first["documents_total"] == 2, "Staged snapshot was incorrect.")
            _require(engine.resolve_search_index() == first["index"], "Scratch alias migration failed.")
            _require(publication.checked_count(es, index=base) == 3, "Retained source changed.")
            _require(not cache.set_cached_search("verification", 10, [], generation=old.generation), "Publication did not fence cache.")
            hits = engine.semantic_search("verification", index_name=first["index"])
            _require({hit["name"] for hit in hits} == {"target", "other"}, "Copied vector search was incorrect.")
            checks.append("migration_stale_id_removal_copy_vectors_and_cache_fence")

            state["lose_ack"] = True
            try:
                engine.index_repository_in_elasticsearch(db, repos[0].id)
            except TimeoutError:
                pass
            else:
                raise RuntimeError("Lost-ack injection did not stop publication.")
            job = db.get(IndexPublicationJob, 1, populate_existing=True)
            _require(job is not None and job.phase == "ready", "Lost acknowledgement did not retain ready work.")
            active = engine.resolve_search_index()
            before_retry = dict(state)
            retry = engine.index_repository_in_elasticsearch(db, repos[0].id)
            _require(retry["resumed"] and retry["index"] == active and state == before_retry,
                     "Lost-ack retry rebuilt or switched the active stage.")
            checks.append("lost_alias_ack_recovery_without_rebuild")

            with patch.object(publication, "invalidate_search_cache", side_effect=RuntimeError("Injected cache failure.")):
                try:
                    engine.index_repository_in_elasticsearch(db, repos[0].id)
                except RuntimeError as exc:
                    _require(str(exc) == "Injected cache failure.", "Unexpected publication failure.")
                else:
                    raise RuntimeError("Cache-failure injection did not stop finalization.")
            job = db.get(IndexPublicationJob, 1, populate_existing=True)
            _require(job is not None and job.phase == "published", "Cache failure did not retain published work.")
            active = engine.resolve_search_index()
            before_retry = dict(state)
            retry = engine.index_repository_in_elasticsearch(db, repos[0].id)
            _require(retry["resumed"] and retry["index"] == active and state == before_retry,
                     "Cache retry rebuilt or switched the active stage.")
            checks.append("published_cache_failure_recovery_without_rebuild")

            db.execute(delete(CodeSymbol).where(CodeSymbol.repository_id == repos[0].id))
            db.commit()
            empty = engine.index_repository_in_elasticsearch(db, repos[0].id)
            _require(empty["symbols_indexed"] == 0 and empty["documents_total"] == 1, "Empty snapshot replacement failed.")
            hits = engine.semantic_search("verification", index_name=empty["index"])
            _require([hit["name"] for hit in hits] == ["other"], "Empty replacement changed unaffected repository.")
            _require(db.get(IndexPublicationJob, 1) is None and db.get(RepositoryFullIndexJob, repos[0].id) is None,
                     "Publication journals were not finalized.")
            _require(repos[0].last_indexed_commit == "unchanged", "Full publication changed Git checkpoint.")
            checks.append("empty_replacement_and_checkpoint_preservation")

            engine.delete_paths_from_elasticsearch(repos[1].id, [files[1].path])
            _require(publication.checked_count(es, index=empty["index"]) == 0, "Incremental delete failed.")
            _require(engine.index_files_in_elasticsearch(db, [files[1].id]) == 1, "Incremental write failed.")
            _require(publication.checked_count(es, index=empty["index"]) == 1, "Incremental refresh failed.")
            checks.append("real_incremental_delete_bulk_and_refresh_responses")
    finally:
        try:
            try:
                db.rollback()
                identities.update({row.index_name: row.index_uuid for row in db.query(SearchIndexGeneration)
                                   if row.index_uuid is not None})
                _clear_indices(es, base, identities)
                _require(alias_target(es, alias_name=alias) is None, "Scratch alias cleanup was incomplete.")
            finally:
                _clear_namespace(redis, prefix)
            _require(_normal_snapshot(es, normal_legacy, normal_alias) == normal, "Normal index snapshot changed.")
            _require(redis.get(normal_generation_key) == normal_generation, "Normal cache generation changed.")
        finally:
            try:
                db.close()
            finally:
                sql.dispose()
    return {"status": "passed", "checks": checks, "artifacts_removed": True,
            "normal_routing_uuid_count_and_generation_unchanged": True,
            "metadata": "temporary SQLite", "embeddings": "deterministic vectors"}


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        report = verify_publication_protocol(engine.elasticsearch_client, cache.redis_client,
            on_namespace=lambda namespace: print(json.dumps({"status": "starting", **namespace}), flush=True))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__,
            "detail": "Verification or cleanup failed; inspect the printed scratch namespace before retrying."}))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
