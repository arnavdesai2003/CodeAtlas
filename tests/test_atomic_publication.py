"""Temporary SQL transactions, deterministic alias transitions, no live services."""
import os
import unittest
from unittest.mock import Mock, patch

os.environ.update(
    DATABASE_URL="postgresql+psycopg://test:test@127.0.0.1/test",
    ELASTICSEARCH_URL="http://127.0.0.1:9200", REDIS_URL="redis://127.0.0.1:6379/15",
    HF_HUB_OFFLINE="1",
)
from elasticsearch import NotFoundError
from elastic_transport import ApiResponseMeta, NodeConfig, ObjectApiResponse
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app.db.database import Base
from app.db.models import Repository, CodeFile, CodeSymbol, RepositoryFullIndexJob, IndexPublicationJob, SearchIndexGeneration
from app.search import engine, publication
from app.search.indexes import alias_target
from app.indexer.locking import RepositorySyncInProgress, repository_sync_lock
from app.search.errors import InvalidSearchResponseError


def wrapped(body):
    return ObjectApiResponse(body=body, meta=ApiResponseMeta(
        200, "1.1", {}, 0, NodeConfig("http", "localhost", 9200),
    ))


def missing_alias():
    return NotFoundError("missing", meta=ApiResponseMeta(404, "1.1", {}, 0,
        NodeConfig("http", "localhost", 9200)), body={})


class RoutingTests(unittest.TestCase):
    def test_wrapped_alias_response_pins_a_real_client_target(self):
        client = Mock()
        client.indices.get_alias.return_value = wrapped({"generation": {"aliases": {"search": {}}}})
        self.assertEqual(alias_target(client, alias_name="search"), "generation")
        client.indices.get_alias.assert_called_once_with(name="search")

    def test_malformed_aliases_raise_typed_errors_instead_of_selecting_targets(self):
        client = Mock()
        for body in (None, ["generation"], {"": {"aliases": {"search": {}}}},
                     {"generation": None}, {"generation": {"aliases": []}},
                     {"generation": {"aliases": {"other": {}}}},
                     {"generation": {"aliases": {"search": None}}}):
            for response in (body, wrapped(body)):
                with self.subTest(body=body, wrapped=isinstance(response, ObjectApiResponse)):
                    client.indices.get_alias.return_value = response
                    with self.assertRaises(InvalidSearchResponseError):
                        alias_target(client, alias_name="search")

    def test_missing_generation_blocks_incremental_writes_without_recreation(self):
        provision = engine.create_symbol_index
        with patch.object(engine, "elasticsearch_client") as es, \
             patch.object(engine, "resolve_search_index", return_value="missing_generation"), \
             patch.object(engine, "bulk") as bulk, \
             patch.object(engine, "create_symbol_index", side_effect=lambda **kwargs: provision(es, **kwargs)):
            es.indices.exists.return_value = False
            for operation in (lambda: engine.delete_paths_from_elasticsearch(1, ["main.py"]),
                              lambda: engine.index_files_in_elasticsearch(Mock(), [1])):
                with self.assertRaisesRegex(RuntimeError, "generation is missing"):
                    operation()
            es.indices.create.assert_not_called()
            es.options.return_value.delete_by_query.assert_not_called()
            bulk.assert_not_called()

    def test_bootstrap_requires_real_boolean_acknowledgements_and_preserves_index(self):
        client = Mock()
        client.indices.exists.return_value = False
        for body in ({}, None, {"acknowledged": 1, "shards_acknowledged": True},
                     {"acknowledged": True, "shards_acknowledged": "true"}):
            client.indices.create.return_value = wrapped(body)
            with self.subTest(body=body), self.assertRaisesRegex(RuntimeError, "not acknowledged"):
                engine.create_symbol_index(client, index_name=engine.INDEX_NAME)
        client.indices.delete.assert_not_called()
        client.indices.create.return_value = wrapped({"acknowledged": True, "shards_acknowledged": True})
        engine.create_symbol_index(client, index_name=engine.INDEX_NAME)

    def test_close_search_connections_preserves_hits_queries_and_generation(self):
        hit = {"_score": 1.5, "_source": {
            "repository": "repo", "path": "m.py", "name": "f",
            "qualified_name": "f", "kind": "function", "start_line": 1,
            "end_line": 2, "code": "def f(): pass"}}
        observations = []
        for enabled in (False, True):
            with self.subTest(enabled=enabled), \
                 patch.object(engine.settings, "elasticsearch_close_search_connections", enabled), \
                 patch.object(engine, "elasticsearch_client") as es, \
                 patch.object(engine, "embed_text", return_value=[.1]*384):
                es.options.return_value = es
                es.indices.get_alias.return_value = {"pinned": {"aliases": {engine.SEARCH_ALIAS: {}}}}
                es.search.return_value = {"hits": {"hits": [hit]}}
                result = engine.hybrid_search_weighted("q", 10)
                self.assertEqual(es.indices.get_alias.call_count, 1)
                calls = sorted([c.kwargs for c in es.search.call_args_list], key=lambda c: "knn" in c)
                self.assertEqual([c["index"] for c in calls], ["pinned", "pinned"])
                headers = [c.kwargs["headers"] for c in es.options.call_args_list if "headers" in c.kwargs]
                self.assertEqual(headers, [{"connection": "close"}]*2 if enabled else [])
                observations.append((result, calls))
        self.assertEqual(observations[0], observations[1])

    def test_close_search_connections_does_not_hide_transport_failure(self):
        with patch.object(engine.settings, "elasticsearch_close_search_connections", True), \
             patch.object(engine, "elasticsearch_client") as es:
            es.options.return_value = es
            es.search.side_effect = ConnectionError("outage")
            with self.assertRaises(ConnectionError):
                engine.bm25_search("q", index_name="pinned")
            es.indices.get_alias.assert_not_called()

    def test_missing_alias_falls_back_to_legacy_without_creating_index(self):
        with patch.object(engine, "elasticsearch_client") as es:
            es.indices.get_alias.side_effect = missing_alias()
            self.assertEqual(engine.resolve_search_index(), engine.INDEX_NAME)
            es.indices.create.assert_not_called()

    def test_alias_requires_single_target_and_does_not_hide_outages(self):
        client = Mock()
        for response in ({}, {"a": {}, "b": {}}):
            client.indices.get_alias.return_value = response
            with self.assertRaises(RuntimeError):
                alias_target(client, alias_name="search")
        client.indices.get_alias.side_effect = ConnectionError("outage")
        with self.assertRaises(ConnectionError):
            alias_target(client, alias_name="search")

    def test_hybrid_pins_both_branches_across_alias_change(self):
        with patch.object(engine, "elasticsearch_client") as es, \
             patch.object(engine, "embed_text", return_value=[.1]*384):
            active = ["before"]
            es.indices.get_alias.side_effect = lambda **kwargs: {
                active[0]: {"aliases": {engine.SEARCH_ALIAS: {}}},
            }
            def search(**kwargs):
                active[0] = "after"
                return {"hits": {"hits": []}}
            es.options.return_value.search.side_effect = search
            self.assertEqual(engine.hybrid_search_weighted("q"), [])
            self.assertEqual(es.indices.get_alias.call_count, 1)
            self.assertEqual([call.kwargs["index"] for call in es.options.return_value.search.call_args_list],
                             ["before", "before"])

    def test_incremental_writes_follow_the_published_generation(self):
        with patch.object(engine, "elasticsearch_client") as es, \
             patch.object(engine, "create_symbol_index"):
            es.indices.get_alias.return_value = {"generation": {"aliases": {engine.SEARCH_ALIAS: {}}}}
            es.options.return_value.delete_by_query.return_value = {
                "timed_out": False, "total": 0, "deleted": 0,
                "failures": [], "version_conflicts": 0,
            }
            engine.delete_paths_from_elasticsearch(1, ["main.py"])
            self.assertEqual(es.options.return_value.delete_by_query.call_args.kwargs["index"], "generation")


class AtomicPublicationTests(unittest.TestCase):
    def test_full_publication_accepts_real_client_wrappers_through_finalization(self):
        for endpoint in (self.es.indices.get_alias, self.es.indices.create,
                         self.es.indices.get_settings, self.es.indices.get_mapping,
                         self.es.count, self.es.reindex, self.es.indices.refresh,
                         self.es.indices.update_aliases):
            effect, result = endpoint.side_effect, endpoint.return_value
            endpoint.side_effect = lambda *args, _effect=effect, _result=result, **kwargs: wrapped(
                _effect(*args, **kwargs) if _effect is not None else _result)
        result = self.publish()
        self.assertEqual(result["documents_total"], 3)
        self.assertEqual(self.alias, result["index"])
        self.assertIsNone(self.journal())
        self.invalidate.assert_called_once_with(strict=True)

    def test_malformed_copy_keeps_building_journal_without_target_writes(self):
        good = {"timed_out": False, "failures": [], "version_conflicts": 0, "total": 2, "created": 2}
        bodies = [None, {}, *[{**good, field: value} for field, value in (
            ("timed_out", 0), ("failures", None), ("failures", {}),
            ("version_conflicts", False), ("total", 2.0), ("created", 2.0),
        )]]
        for body in bodies:
            with self.subTest(body=body):
                self.es.reindex.return_value = wrapped(body)
                with self.assertRaises(RuntimeError):
                    self.publish()
                self.assertEqual(self.journal()[0], "building")
                self.write.assert_not_called()
                self.es.indices.update_aliases.assert_not_called()
                self.invalidate.assert_not_called()
        self.es.reindex.return_value = wrapped(good)
        self.assertTrue(self.publish()["resumed"])
        self.assertIsNone(self.journal())

    def test_malformed_swap_ack_retries_active_stage_without_rebuilding(self):
        def ambiguous_swap(**kwargs):
            self.swap(**kwargs)
            return wrapped({"acknowledged": 1})
        self.es.indices.update_aliases.side_effect = ambiguous_swap
        with self.assertRaisesRegex(RuntimeError, "not acknowledged"):
            self.publish()
        stage = self.alias
        self.assertEqual(self.journal()[0], "ready")
        self.invalidate.assert_not_called()
        self.assertEqual(self.publish()["index"], stage)
        self.assertIsNone(self.journal())
        self.es.reindex.assert_called_once()
        self.es.indices.update_aliases.assert_called_once()

    def test_optional_alias_errors_must_be_false_before_finalization(self):
        for value in (0, None, [], "false", True):
            with self.subTest(value=value):
                self.es.indices.update_aliases.side_effect = lambda **kwargs: wrapped({
                    "acknowledged": True, "errors": value,
                })
                with self.assertRaisesRegex(RuntimeError, "not acknowledged"):
                    self.publish()
                self.assertEqual(self.journal()[0], "ready")
                self.invalidate.assert_not_called()
        self.es.indices.update_aliases.side_effect = lambda **kwargs: wrapped({
            **self.swap(**kwargs), "errors": False,
        })
        self.assertTrue(self.publish()["resumed"])

    def test_malformed_wrapped_count_and_refresh_stop_publication(self):
        for body in (None, {"count": 2, "_shards": None},
                     {"count": 2, "_shards": {"failed": 0}, "timed_out": 0}):
            self.es.count.side_effect = None
            self.es.count.return_value = wrapped(body)
            with self.subTest(body=body), self.assertRaisesRegex(RuntimeError, "count was incomplete"):
                self.publish()
            self.es.reindex.assert_not_called()
            self.invalidate.assert_not_called()
        for body in (None, {"_shards": []}, {"_shards": {"failed": False}}):
            with self.subTest(refresh=body):
                self.es.indices.refresh.return_value = wrapped(body)
                with self.assertRaisesRegex(RuntimeError, "refresh was incomplete"):
                    engine.refresh_symbol_index("stage", client=self.es)

    def setUp(self):
        self.sql = create_engine("sqlite://")
        self.addCleanup(self.sql.dispose)
        @event.listens_for(self.sql, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.sql)
        self.sessions = sessionmaker(self.sql, autoflush=False, expire_on_commit=False)
        self.db = self.sessions()
        self.addCleanup(self.db.close)
        self.repo = Repository(name="demo", clone_url="https://github.com/test/demo", last_indexed_commit="old")
        self.other = Repository(name="other", clone_url="https://github.com/test/other", last_indexed_commit="other")
        self.db.add_all([self.repo, self.other])
        self.db.flush()
        file = CodeFile(repository_id=self.repo.id, path="main.py", language="python")
        self.db.add(file)
        self.db.flush()
        self.db.add(CodeSymbol(repository_id=self.repo.id, file_id=file.id, name="run", qualified_name="run",
            kind="function", start_line=1, end_line=1, code="def run(): pass"))
        self.db.add(RepositoryFullIndexJob(repository_id=self.repo.id, stats={"symbols_indexed": 1}))
        self.db.commit()
        self.alias = None
        self.stages = set()
        self.es = patch.object(engine, "elasticsearch_client").start()
        self.es.options.return_value = self.es
        self.es.indices.refresh.return_value = {"_shards": {"failed": 0}}
        self.es.indices.get_mapping.side_effect = lambda **kw: {kw["index"]: {
            "mappings": {"properties": {"embedding": {"type": "dense_vector", "dims": 384,
                "index_options": {"type": "bbq_hnsw"}}}}}}
        self.es.indices.get_settings.side_effect = lambda **kw: {kw["index"]: {
            "settings": {"index": {"number_of_shards": "1", "number_of_replicas": "0",
                "analysis": {"analyzer": {"custom": {"type": "standard"}}}, "uuid": "omit"}}}}
        def create_stage(**kwargs):
            self.stages.add(kwargs["index"])
            return {"acknowledged": True, "shards_acknowledged": True}
        self.es.indices.create.side_effect = create_stage
        self.create = patch.object(engine, "create_symbol_index", side_effect=lambda **kw:
                                  self.stages.add(kw["index_name"])).start()
        self.write = patch.object(engine, "_write_repository_index", return_value={"symbols_indexed": 1}).start()
        self.invalidate = patch.object(publication, "invalidate_search_cache", return_value=0).start()
        self.addCleanup(patch.stopall)
        def get_alias(**kwargs):
            if self.alias is None:
                raise missing_alias()
            return {self.alias: {"aliases": {engine.SEARCH_ALIAS: {}}}}
        def swap(**kwargs):
            self.alias = kwargs["actions"][-1]["add"]["index"]
            return {"acknowledged": True}
        self.swap = swap
        self.es.indices.get_alias.side_effect = get_alias
        self.es.indices.update_aliases.side_effect = swap
        self.es.count.side_effect = lambda **kwargs: {"count": 2 if "query" in kwargs else 3, "_shards": {"failed": 0}}
        self.es.reindex.return_value = {
            "timed_out": False, "failures": [], "version_conflicts": 0,
            "total": 2, "created": 2,
        }

    def publish(self, db=None):
        return engine.index_repository_in_elasticsearch(db or self.db, self.repo.id)

    def journal(self):
        with self.sessions() as db:
            row = db.get(IndexPublicationJob, 1)
            return None if row is None else (row.phase, row.staging_index, dict(row.stats))

    def test_initial_migration_is_add_only_and_retains_legacy_index(self):
        result = self.publish()
        actions = self.es.indices.update_aliases.call_args.kwargs["actions"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(result["source_index"], engine.INDEX_NAME)
        self.assertEqual(result["documents_total"], 3)
        self.assertEqual(result["publication"], "atomic_alias")
        self.assertEqual(self.alias, result["index"])
        self.es.indices.delete.assert_not_called()
        self.es.delete_by_query.assert_not_called()
        self.assertIsNone(self.journal())

    def test_unacknowledged_stage_creation_stops_before_copy_and_retry_uses_fresh_name(self):
        def ambiguous_create(**kwargs):
            self.stages.add(kwargs["index"])
            return {"acknowledged": False, "shards_acknowledged": False}
        self.es.indices.create.side_effect = ambiguous_create
        with self.assertRaisesRegex(RuntimeError, "creation was not acknowledged"):
            self.publish()
        phase, failed_stage, _ = self.journal()
        self.assertEqual(phase, "building")
        self.assertIn(failed_stage, self.stages)
        self.es.reindex.assert_not_called()
        self.write.assert_not_called()
        self.es.indices.update_aliases.assert_not_called()
        self.invalidate.assert_not_called()
        self.es.indices.create.side_effect = lambda **kwargs: {
            "acknowledged": True, "shards_acknowledged": True,
        }
        with self.sessions() as retry:
            result = self.publish(retry)
            failed = retry.get(SearchIndexGeneration, failed_stage)
            self.assertEqual(failed.state, "abandoned")
            self.assertIsNone(failed.index_uuid)
        self.assertTrue(result["resumed"])
        self.assertNotEqual(result["index"], failed_stage)
        self.assertIn(failed_stage, self.stages)
        self.es.indices.delete.assert_not_called()
        self.es.reindex.assert_called_once()

    def test_staging_creation_requires_both_boolean_acknowledgements(self):
        self.es.indices.create.side_effect = None
        for response in ({}, {"acknowledged": True},
                         {"acknowledged": True, "shards_acknowledged": False},
                         {"acknowledged": 1, "shards_acknowledged": True}):
            with self.subTest(response=response):
                self.es.indices.create.return_value = response
                with self.assertRaisesRegex(RuntimeError, "creation was not acknowledged"):
                    publication.create_staging_index(self.es, source="source", stage="stage")

    def test_failed_stage_refresh_blocks_publication_and_rebuilds_on_retry(self):
        self.es.indices.refresh.return_value = {"_shards": {"failed": 1}}
        with self.assertRaisesRegex(RuntimeError, "refresh was incomplete"):
            self.publish()
        self.assertEqual(self.journal()[0], "building")
        failed_stage = self.journal()[1]
        self.es.indices.update_aliases.assert_not_called()
        self.invalidate.assert_not_called()
        self.es.indices.refresh.return_value = {"_shards": {"failed": 0}}
        with self.sessions() as retry:
            result = self.publish(retry)
        self.assertTrue(result["resumed"])
        self.assertNotEqual(result["index"], failed_stage)
        self.assertEqual(self.write.call_count, 2)
        self.es.indices.update_aliases.assert_called_once()
        self.invalidate.assert_called_once_with(strict=True)

    def test_refresh_requires_explicit_zero_failed_shards(self):
        for response in ({}, {"_shards": {}}, {"_shards": {"failed": True}},
                         {"_shards": {"failed": -1}}, {"_shards": {"failed": "0"}}):
            with self.subTest(response=response):
                self.es.indices.refresh.return_value = response
                with self.assertRaisesRegex(RuntimeError, "refresh was incomplete"):
                    engine.refresh_symbol_index("stage")

    def test_success_records_identity_and_subsequent_retirement(self):
        first = self.publish()["index"]
        with self.sessions() as db:
            row = db.get(SearchIndexGeneration, first)
            self.assertEqual(row.state, "published")
            self.assertEqual(row.index_uuid, "omit")
            self.assertIsNotNone(row.published_at)
            self.assertIsNone(row.inactive_at)
        second = self.publish()["index"]
        with self.sessions() as db:
            self.assertEqual(db.get(SearchIndexGeneration, second).state, "published")
            old = db.get(SearchIndexGeneration, first)
            self.assertEqual(old.state, "retired")
            self.assertGreaterEqual(old.inactive_at, old.published_at)
            self.assertIsNone(db.get(SearchIndexGeneration, engine.INDEX_NAME))

    def test_cache_recovery_does_not_extend_retirement_grace(self):
        first = self.publish()["index"]
        self.invalidate.side_effect = RuntimeError("Redis outage")
        with self.assertRaises(RuntimeError):
            self.publish()
        with self.sessions() as db:
            retirement = db.get(SearchIndexGeneration, first).inactive_at
        self.invalidate.side_effect = None
        self.publish()
        with self.sessions() as db:
            self.assertEqual(db.get(SearchIndexGeneration, first).inactive_at, retirement)

    def test_ready_stage_identity_change_blocks_alias_switch(self):
        self.es.indices.update_aliases.side_effect = TimeoutError("before switch")
        with self.assertRaises(TimeoutError):
            self.publish()
        self.es.indices.update_aliases.reset_mock()
        self.es.indices.get_settings.side_effect = lambda **kw: {kw["index"]: {
            "settings": {"index": {"uuid": "recreated"}}}}
        with self.assertRaisesRegex(RuntimeError, "another index identity"):
            self.publish()
        self.es.indices.update_aliases.assert_not_called()
        self.assertIsNone(self.alias)

    def test_upgrade_adopts_untracked_ready_stage_and_source_conservatively(self):
        self.alias = f"{engine.INDEX_NAME}_generation_{'a'*32}"
        source = self.alias
        self.es.indices.update_aliases.side_effect = TimeoutError("before switch")
        with self.assertRaises(TimeoutError):
            self.publish()
        self.db.query(SearchIndexGeneration).delete()
        self.db.commit()
        self.es.indices.update_aliases.side_effect = self.swap
        result = self.publish()
        self.assertEqual(self.write.call_count, 1)
        self.assertEqual(self.db.get(SearchIndexGeneration, source).state, "retired")
        self.assertIsNotNone(self.db.get(SearchIndexGeneration, source).inactive_at)
        self.assertEqual(self.db.get(SearchIndexGeneration, result["index"]).state, "published")

    def test_failed_identity_commit_keeps_abandoned_attempt_unverified(self):
        commit = self.db.commit
        calls = 0
        def fail_identity():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("identity commit")
            commit()
        with patch.object(self.db, "commit", side_effect=fail_identity):
            with self.assertRaises(RuntimeError):
                self.publish()
        first = self.journal()[1]
        with self.sessions() as db:
            self.assertIsNone(db.get(SearchIndexGeneration, first).index_uuid)
        self.publish()
        with self.sessions() as db:
            row = db.get(SearchIndexGeneration, first)
            self.assertEqual(row.state, "abandoned")
            self.assertIsNotNone(row.inactive_at)
            self.assertIsNone(row.index_uuid)

    def test_existing_alias_switch_uses_exact_source_and_required_remove(self):
        self.alias = "existing_generation"
        self.publish()
        actions = self.es.indices.update_aliases.call_args.kwargs["actions"]
        self.assertEqual(actions[0], {"remove": {"index": "existing_generation",
            "alias": engine.SEARCH_ALIAS, "must_exist": True}})
        self.assertEqual(len(actions), 2)

    def test_failed_build_does_not_publish_and_retry_uses_fresh_stage(self):
        self.write.side_effect = RuntimeError("bulk failed")
        with self.assertRaises(RuntimeError):
            self.publish()
        first = self.journal()[1]
        self.assertIsNone(self.alias)
        self.es.indices.update_aliases.assert_not_called()
        self.write.side_effect = None
        with self.sessions() as retry:
            result = self.publish(retry)
        self.assertNotEqual(first, result["index"])
        self.assertIn(first, self.stages)
        self.es.indices.delete.assert_not_called()
        with self.sessions() as db:
            row = db.get(SearchIndexGeneration, first)
            self.assertEqual(row.state, "abandoned")
            self.assertIsNotNone(row.inactive_at)
            self.assertEqual(row.index_uuid, "omit")

    def test_copy_count_mismatch_keeps_old_index(self):
        self.es.reindex.return_value = {
            "timed_out": False, "failures": [], "version_conflicts": 0,
            "total": 2, "created": 1,
        }
        with self.assertRaisesRegex(RuntimeError, "copy count"):
            self.publish()
        self.assertIsNone(self.alias)
        self.write.assert_not_called()
        self.assertEqual(self.journal()[0], "building")

    def test_partial_source_count_stops_before_copy_and_can_retry(self):
        self.es.count.side_effect = lambda **kwargs: {"count": 2, "_shards": {"failed": 1}}
        with self.assertRaisesRegex(RuntimeError, "count was incomplete"):
            self.publish()
        self.assertEqual(self.journal()[0], "building")
        failed_stage = self.journal()[1]
        self.es.reindex.assert_not_called()
        self.write.assert_not_called()
        self.invalidate.assert_not_called()
        self.es.count.side_effect = lambda **kwargs: {
            "count": 2 if "query" in kwargs else 3, "_shards": {"failed": 0},
        }
        with self.sessions() as retry:
            result = self.publish(retry)
        self.assertTrue(result["resumed"])
        self.assertNotEqual(result["index"], failed_stage)
        self.es.indices.update_aliases.assert_called_once()

    def test_partial_ready_count_blocks_alias_switch_without_rebuild(self):
        self.es.indices.update_aliases.side_effect = TimeoutError("before switch")
        with self.assertRaises(TimeoutError):
            self.publish()
        stage = self.journal()[1]
        self.es.indices.update_aliases.reset_mock()
        self.es.indices.update_aliases.side_effect = self.swap
        self.es.count.side_effect = lambda **kwargs: {"count": 3, "_shards": {"failed": 1}}
        with self.sessions() as retry:
            with self.assertRaisesRegex(RuntimeError, "count was incomplete"):
                self.publish(retry)
        self.assertEqual(self.journal()[0], "ready")
        self.es.indices.update_aliases.assert_not_called()
        self.invalidate.assert_not_called()
        self.es.count.side_effect = lambda **kwargs: {"count": 3, "_shards": {"failed": 0}}
        with self.sessions() as retry:
            self.assertEqual(self.publish(retry)["index"], stage)
        self.write.assert_called_once()
        self.es.reindex.assert_called_once()

    def test_count_requires_valid_integer_and_complete_shard_response(self):
        self.es.count.side_effect = None
        for response in ({"count": 3}, {"count": 3, "_shards": {"failed": False}},
                         {"count": True, "_shards": {"failed": 0}},
                         {"count": -1, "_shards": {"failed": 0}},
                         {"count": 3.0, "_shards": {"failed": 0}},
                         {"count": 3, "_shards": {"failed": 0}, "timed_out": True}):
            with self.subTest(response=response):
                self.es.count.return_value = response
                with self.assertRaises(RuntimeError):
                    publication.checked_count(self.es, index="stage")
        self.es.count.return_value = {"count": 0, "_shards": {"failed": 0}}
        self.assertEqual(publication.checked_count(self.es, index="empty"), 0)

    def test_staged_count_mismatch_prevents_alias_switch(self):
        self.es.count.side_effect = lambda **kwargs: {"count": 2, "_shards": {"failed": 0}}
        with self.assertRaisesRegex(RuntimeError, "Staging index count"):
            self.publish()
        self.assertIsNone(self.alias)
        self.es.indices.update_aliases.assert_not_called()

    def test_lost_alias_ack_is_recovered_without_rebuilding_published_stage(self):
        def lost_ack(**kwargs):
            self.swap(**kwargs)
            raise TimeoutError("lost acknowledgement")
        self.es.indices.update_aliases.side_effect = lost_ack
        with self.assertRaises(TimeoutError):
            self.publish()
        self.assertEqual(self.journal()[0], "ready")
        self.es.indices.update_aliases.side_effect = self.swap
        with self.sessions() as retry:
            result = self.publish(retry)
        self.assertEqual(self.write.call_count, 1)
        self.assertEqual(self.es.indices.update_aliases.call_count, 1)
        self.assertTrue(result["resumed"])
        self.assertIsNone(self.journal())

    def test_unacknowledged_swap_is_inspected_on_retry(self):
        def unacknowledged(**kwargs):
            self.swap(**kwargs)
            return {"acknowledged": False}
        self.es.indices.update_aliases.side_effect = unacknowledged
        with self.assertRaisesRegex(RuntimeError, "not acknowledged"):
            self.publish()
        self.assertIsNotNone(self.alias)
        self.publish()
        self.assertEqual(self.write.call_count, 1)

    def test_cache_outage_preserves_published_stage_and_blocks_other_writers(self):
        self.invalidate.side_effect = RuntimeError("Redis outage")
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(self.journal()[0], "published")
        for repo_id, is_publication in ((self.repo.id, False), (self.other.id, False), (self.other.id, True)):
            with self.subTest(repo_id=repo_id, publication=is_publication):
                with self.assertRaises(RepositorySyncInProgress):
                    with repository_sync_lock(self.db, repo_id, publication=is_publication):
                        self.fail("pending publication must exclude writers")
        self.invalidate.side_effect = None
        self.publish()
        self.assertEqual(self.write.call_count, 1)
        self.assertIsNone(self.journal())

    def test_failed_ready_commit_does_not_switch_alias(self):
        commit = self.db.commit
        calls = 0
        def fail_ready():
            nonlocal calls
            calls += 1
            if calls == 4:
                raise RuntimeError("ready commit")
            commit()
        with patch.object(self.db, "commit", side_effect=fail_ready):
            with self.assertRaises(RuntimeError):
                self.publish()
        self.assertEqual(self.journal()[0], "building")
        self.assertIsNone(self.alias)
        self.publish()
        self.assertEqual(self.write.call_count, 2)

    def test_failed_final_commit_retries_rotation_without_rebuilding(self):
        commit = self.db.commit
        calls = 0
        def fail_final():
            nonlocal calls
            calls += 1
            if calls == 6:
                raise RuntimeError("final commit")
            commit()
        with patch.object(self.db, "commit", side_effect=fail_final):
            with self.assertRaises(RuntimeError):
                self.publish()
        self.assertEqual(self.journal()[0], "published")
        self.publish()
        self.assertEqual(self.write.call_count, 1)
        self.assertEqual(self.invalidate.call_count, 2)
        self.assertIsNone(self.journal())

    def test_failed_initial_journal_commit_does_not_start_staging(self):
        with patch.object(self.db, "commit", side_effect=RuntimeError("journal commit")):
            with self.assertRaises(RuntimeError):
                self.publish()
        self.write.assert_not_called()
        self.es.reindex.assert_not_called()
        self.assertIsNone(self.journal())

    def lose_commit_ack(self, boundary):
        commit = self.db.commit
        calls = 0

        def committed_but_lost():
            nonlocal calls
            calls += 1
            commit()
            if calls == boundary:
                raise RuntimeError("lost publication commit acknowledgement")

        with patch.object(self.db, "commit", side_effect=committed_but_lost):
            with self.assertRaisesRegex(RuntimeError, "lost publication"):
                self.publish()
        self.db.close()

    def test_lost_ready_ack_reuses_validated_stage_in_new_session(self):
        self.lose_commit_ack(4)
        phase, stage, _ = self.journal()
        self.assertEqual(phase, "ready")
        self.assertIsNone(self.alias)
        with self.sessions() as retry:
            result = self.publish(retry)
            self.assertTrue(result["resumed"])
            self.assertEqual(result["index"], stage)
        self.es.reindex.assert_called_once()
        self.write.assert_called_once()
        self.es.indices.update_aliases.assert_called_once()
        self.invalidate.assert_called_once_with(strict=True)
        self.assertIsNone(self.journal())

    def test_lost_published_ack_only_finalizes_in_new_session(self):
        self.lose_commit_ack(5)
        phase, stage, _ = self.journal()
        self.assertEqual(phase, "published")
        self.assertEqual(self.alias, stage)
        self.invalidate.assert_not_called()
        with self.sessions() as retry:
            published_at = retry.get(SearchIndexGeneration, stage).published_at
            result = self.publish(retry)
            self.assertTrue(result["resumed"])
            self.assertEqual(retry.get(SearchIndexGeneration, stage).published_at, published_at)
            self.assertIsNone(retry.get(RepositoryFullIndexJob, self.repo.id))
        self.es.reindex.assert_called_once()
        self.write.assert_called_once()
        self.es.indices.update_aliases.assert_called_once()
        self.invalidate.assert_called_once_with(strict=True)
        self.assertIsNone(self.journal())

    def test_lost_final_ack_leaves_completed_publication_durable(self):
        self.lose_commit_ack(6)
        self.assertIsNone(self.journal())
        with self.sessions() as verify:
            self.assertIsNone(verify.get(RepositoryFullIndexJob, self.repo.id))
            generation = verify.get(SearchIndexGeneration, self.alias)
            self.assertEqual(generation.state, "published")
            self.assertIsNotNone(generation.published_at)
            self.assertEqual(verify.get(Repository, self.repo.id).last_indexed_commit, "old")
        self.es.reindex.assert_called_once()
        self.write.assert_called_once()
        self.es.indices.update_aliases.assert_called_once()
        self.invalidate.assert_called_once_with(strict=True)
        self.es.indices.delete.assert_not_called()

    def test_ready_stage_is_revalidated_before_retrying_alias_switch(self):
        self.es.indices.update_aliases.side_effect = TimeoutError("before switch")
        with self.assertRaises(TimeoutError):
            self.publish()
        self.assertEqual(self.journal()[0], "ready")
        self.es.indices.update_aliases.reset_mock()
        self.es.count.side_effect = None
        self.es.count.return_value = {"count": 0, "_shards": {"failed": 0}}
        with self.assertRaisesRegex(RuntimeError, "Prepared staging index changed"):
            self.publish()
        self.es.indices.update_aliases.assert_not_called()
        self.assertEqual(self.write.call_count, 1)

    def test_external_alias_change_requires_reconciliation(self):
        self.write.side_effect = RuntimeError("stop build")
        with self.assertRaises(RuntimeError):
            self.publish()
        self.alias = "external"
        self.write.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "outside the publication protocol"):
            self.publish()
        self.write.assert_not_called()

    def test_other_repository_cannot_resume_pending_publication(self):
        self.write.side_effect = RuntimeError("stop")
        with self.assertRaises(RuntimeError):
            self.publish()
        self.write.reset_mock()
        with self.assertRaises(RepositorySyncInProgress):
            engine.index_repository_in_elasticsearch(self.db, self.other.id)
        self.write.assert_not_called()

    def test_incremental_bulk_targets_the_active_generation(self):
        self.alias = "published_generation"
        file_ids = [row.id for row in self.db.query(CodeFile)]
        with patch.object(engine, "embed_texts", return_value=[[.1]*384]), \
             patch.object(engine, "bulk", return_value=(1, [])) as bulk:
            self.assertEqual(engine.index_files_in_elasticsearch(self.db, file_ids), 1)
        self.assertEqual(bulk.call_args.args[1][0]["_index"], "published_generation")

    def test_stage_preserves_effective_mapping_and_analysis_without_index_identity(self):
        self.publish()
        args = self.es.indices.create.call_args.kwargs
        self.assertEqual(args["mappings"]["properties"]["embedding"]["index_options"], {"type": "bbq_hnsw"})
        self.assertIn("analysis", args["settings"])
        self.assertNotIn("uuid", args["settings"])
