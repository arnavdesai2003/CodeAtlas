"""Read-only inventory/plans and maintenance-only, revalidated generation cleanup."""
from dataclasses import asdict, dataclass
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import math

from app.db.models import (
    IndexPublicationJob, RepositoryFullIndexJob, RepositorySyncJob, SearchIndexGeneration,
)
from app.indexer.locking import generation_maintenance_lock
from app.search.engine import INDEX_NAME, SEARCH_ALIAS
from app.search.generation_lifecycle import is_generation_name, utc_now
from app.search.indexes import alias_target
from app.search.responses import response_body


class RetentionBlocked(RuntimeError):
    pass


class GenerationCleanupError(RuntimeError):
    def __init__(self, result):
        self.result = result
        super().__init__(result["detail"])


@dataclass(frozen=True)
class RetentionPolicy:
    min_age_hours: float = 24
    keep_retired: int = 2

    def __post_init__(self):
        if isinstance(self.min_age_hours, bool) or not isinstance(self.min_age_hours, (int, float)):
            raise RetentionBlocked("Minimum inactive age must be a finite number of hours.")
        if not math.isfinite(self.min_age_hours) or not 1 <= self.min_age_hours <= 876000:
            raise RetentionBlocked("Minimum inactive age must be between 1 and 876000 hours.")
        if type(self.keep_retired) is not int or self.keep_retired < 1:
            raise RetentionBlocked("Retention must keep at least one retired published generation.")


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value):
    return _utc(value).isoformat() if value is not None else None


def inspect_generations(db, client, *, policy=None, now=None,
                        legacy_name=INDEX_NAME, alias_name=SEARCH_ALIAS):
    policy = policy or RetentionPolicy()
    now = _utc(now or utc_now())
    cluster_uuid = client.info().get("cluster_uuid")
    if not isinstance(cluster_uuid, str) or not cluster_uuid or cluster_uuid == "_na_":
        raise RetentionBlocked("Elasticsearch did not provide a usable cluster identity.")
    active = alias_target(client, alias_name=alias_name) or legacy_name
    job = db.get(IndexPublicationJob, 1, populate_existing=True)
    sync_ids = [row.repository_id for row in db.query(RepositorySyncJob).populate_existing().all()]
    full_ids = [row.repository_id for row in db.query(RepositoryFullIndexJob).populate_existing().all()]
    records = {row.index_name: row for row in db.query(SearchIndexGeneration).populate_existing().all()}
    expressions = [legacy_name, f"{legacy_name}_generation_*", active]
    if job is not None:
        expressions.extend((job.source_index, job.staging_index))
    metadata = dict(client.indices.get(
        index=sorted(set(expressions)), allow_no_indices=True, ignore_unavailable=True,
        expand_wildcards="all", filter_path="*.aliases,*.settings.index.uuid",
    ))
    stats = {}
    if metadata:
        response = client.indices.stats(index=sorted(metadata), metric=["docs", "store"],
                                        level="indices", expand_wildcards="all", forbid_closed_indices=False)
        if response.get("_shards", {}).get("failed", 0):
            raise RetentionBlocked("Index statistics contain shard failures; inspect service health first.")
        stats = response.get("indices", {})
    issues = []
    if active not in metadata:
        issues.append("The resolved active index is missing; reconcile before cleanup.")
    kept = sorted((row for row in records.values()
        if row.state == "retired" and row.inactive_at is not None and row.index_name in metadata
        and is_generation_name(row.index_name, legacy_name)),
        key=lambda row: (_utc(row.inactive_at), row.index_name), reverse=True)[:policy.keep_retired]
    kept_names = {row.index_name for row in kept}
    names = set(metadata) | set(records)
    if job is not None:
        names.update((job.source_index, job.staging_index))
    rows = []
    for name in sorted(names):
        row = records.get(name)
        meta = metadata.get(name)
        actual_uuid = meta.get("settings", {}).get("index", {}).get("uuid") if meta is not None else None
        aliases = sorted(meta.get("aliases", {})) if meta is not None else []
        reasons = []
        if name == legacy_name:
            reasons.append("legacy index")
        if name == active:
            reasons.append("active index")
        if aliases:
            reasons.append("referenced by an alias")
        if job is not None and name in {job.source_index, job.staging_index}:
            reasons.append("publication journal reference")
        if not is_generation_name(name, legacy_name):
            reasons.append("outside managed generation naming")
        if meta is None:
            reasons.append("index absent")
        if row is None:
            reasons.append("untracked lifecycle")
        else:
            if row.state not in {"retired", "abandoned"}:
                reasons.append(f"lifecycle state {row.state}")
            if row.index_uuid is None:
                reasons.append("unverified recorded identity")
            elif actual_uuid is not None and actual_uuid != row.index_uuid:
                reasons.append("index identity changed")
            inactive = _utc(row.inactive_at)
            if inactive is None:
                reasons.append("inactive timestamp unknown")
            elif now - inactive < timedelta(hours=policy.min_age_hours):
                reasons.append("inactive grace period")
        if not isinstance(actual_uuid, str) or not actual_uuid or actual_uuid == "_na_":
            reasons.append("unverified live identity")
        if name in kept_names:
            reasons.append("retained published generation")
        usage = stats.get(name, {})
        rows.append({
            "index_name": name, "index_uuid": actual_uuid,
            "recorded_uuid": row.index_uuid if row is not None else None,
            "exists": meta is not None, "aliases": aliases,
            "state": row.state if row is not None else "untracked",
            "repository_id": row.repository_id if row is not None else None,
            "created_at": _iso(row.created_at) if row is not None else None,
            "published_at": _iso(row.published_at) if row is not None else None,
            "inactive_at": _iso(row.inactive_at) if row is not None else None,
            "deleted_at": _iso(row.deleted_at) if row is not None else None,
            "documents": usage.get("primaries", {}).get("docs", {}).get("count"),
            "store_bytes": usage.get("total", {}).get("store", {}).get("size_in_bytes"),
            "eligible": not reasons, "protected_reasons": reasons,
        })
    publication = None if job is None else {
        "repository_id": job.repository_id, "phase": job.phase,
        "source_index": job.source_index, "staging_index": job.staging_index,
        "resume_command": f".venv/bin/python -m scripts.index_elasticsearch --repository-id {job.repository_id}",
    }
    return {
        "version": 1, "cluster_uuid": cluster_uuid, "legacy_name": legacy_name,
        "alias_name": alias_name, "active_index": active, "observed_at": _iso(now),
        "policy": asdict(policy), "issues": issues, "publication_job": publication,
        "sync_jobs": sorted(sync_ids), "full_index_jobs": sorted(full_ids), "indices": rows,
    }


def make_cleanup_plan(inventory):
    return {
        "version": 1, "cluster_uuid": inventory["cluster_uuid"],
        "legacy_name": inventory["legacy_name"], "alias_name": inventory["alias_name"],
        "created_at": inventory["observed_at"], "policy": inventory["policy"],
        "issues": inventory["issues"],
        "pending_work": {"publication_job": inventory.get("publication_job"),
                         "sync_jobs": inventory.get("sync_jobs", []),
                         "full_index_jobs": inventory.get("full_index_jobs", [])},
        "candidates": [{key: row[key] for key in ("index_name", "index_uuid", "state", "inactive_at")}
                       for row in inventory["indices"] if row["eligible"]],
    }


def _assert_idle(client):
    # Reader quiescence is an operator prerequisite. Also reject all active ES
    # write tasks, including copies continuing after a publisher has stopped.
    result = client.tasks.list(actions="indices:data/write/*", detailed=False, group_by="nodes")
    if result.get("node_failures") or result.get("task_failures") or "nodes" not in result:
        raise RetentionBlocked("Elasticsearch task inspection was incomplete.")
    if any(node.get("tasks") for node in result["nodes"].values()):
        raise RetentionBlocked("Elasticsearch write tasks are still running; wait before cleanup.")


def _assert_inventory_safe(inventory):
    if inventory["issues"]:
        raise RetentionBlocked(inventory["issues"][0])
    if inventory["publication_job"] or inventory["sync_jobs"] or inventory["full_index_jobs"]:
        raise RetentionBlocked("Pending repository work must be resumed before applying cleanup.")


def _validate_plan(plan, *, legacy_name, alias_name):
    if not isinstance(plan, dict) or type(plan.get("version")) is not int or plan["version"] != 1:
        raise RetentionBlocked("Unsupported cleanup plan format.")
    if plan.get("legacy_name") != legacy_name or plan.get("alias_name") != alias_name:
        raise RetentionBlocked("Cleanup plan belongs to another index namespace.")
    try:
        policy = RetentionPolicy(**plan["policy"])
    except (KeyError, TypeError):
        raise RetentionBlocked("Cleanup plan has an invalid retention policy.") from None
    candidates = plan.get("candidates")
    if not isinstance(candidates, list):
        raise RetentionBlocked("Cleanup plan requires an explicit candidate list.")
    names = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise RetentionBlocked("Invalid cleanup candidate.")
        name = candidate.get("index_name")
        if not isinstance(name, str) or not is_generation_name(name, legacy_name) or name in names:
            raise RetentionBlocked("Cleanup candidates must be unique exact managed generation names.")
        if not isinstance(candidate.get("index_uuid"), str) or not candidate["index_uuid"]:
            raise RetentionBlocked("Cleanup candidate has no reviewed identity.")
        if candidate.get("state") not in {"retired", "abandoned"} or not isinstance(candidate.get("inactive_at"), str):
            raise RetentionBlocked("Cleanup candidate has no reviewed inactive lifecycle.")
        names.add(name)
    return policy, candidates


def _validate_candidate(candidate, inventory):
    row = next((row for row in inventory["indices"] if row["index_name"] == candidate["index_name"]), None)
    if row is None or row["recorded_uuid"] != candidate["index_uuid"]:
        raise RetentionBlocked("Cleanup candidate no longer has its reviewed lifecycle identity.")
    if row["exists"]:
        if not row["eligible"] or row["index_uuid"] != candidate["index_uuid"]:
            raise RetentionBlocked("Cleanup candidate is protected or its live identity changed; replan.")
        if row["state"] != candidate.get("state") or row["inactive_at"] != candidate.get("inactive_at"):
            raise RetentionBlocked("Cleanup candidate lifecycle changed since review; replan.")
    else:
        if row["state"] not in {"retired", "abandoned", "deleted"}:
            raise RetentionBlocked("Missing cleanup candidate has an unexpected lifecycle state.")
        if row["inactive_at"] != candidate["inactive_at"] or (
            row["state"] != "deleted" and row["state"] != candidate["state"]
        ):
            raise RetentionBlocked("Missing cleanup candidate lifecycle changed since review; replan.")
        # A repeated plan may reconcile an acknowledged/ambiguous prior delete.
    return row


def apply_cleanup_plan(db, client, plan, *, quiesced=False, now=None,
                       legacy_name=INDEX_NAME, alias_name=SEARCH_ALIAS):
    result = {"status": "complete", "deleted": [], "already_absent": [],
              "failed_index": None, "error": None, "detail": None}
    try:
        if not quiesced:
            raise RetentionBlocked("Stop all readers and writers, then explicitly attest quiescence to apply cleanup.")
        policy, candidates = _validate_plan(plan, legacy_name=legacy_name, alias_name=alias_name)
        with generation_maintenance_lock(db):
            def fresh_inventory():
                report = inspect_generations(db, client, policy=policy, now=now,
                                            legacy_name=legacy_name, alias_name=alias_name)
                if report["cluster_uuid"] != plan.get("cluster_uuid"):
                    raise RetentionBlocked("Cleanup plan belongs to another Elasticsearch cluster.")
                _assert_inventory_safe(report)
                return report
            report = fresh_inventory()
            # Validate every target before the first destructive operation.
            for candidate in candidates:
                _validate_candidate(candidate, report)
            _assert_idle(client)
            for candidate in candidates:
                result["failed_index"] = candidate["index_name"]
                row = _validate_candidate(candidate, fresh_inventory())
                _assert_idle(client)
                if row["exists"]:
                    response = response_body(client.indices.delete(index=candidate["index_name"]))
                    if not isinstance(response, Mapping) or response.get("acknowledged") is not True:
                        raise RetentionBlocked("Deletion was not acknowledged; inspect its outcome before retrying.")
                    result["deleted"].append(candidate["index_name"])
                else:
                    result["already_absent"].append(candidate["index_name"])
                record = db.get(SearchIndexGeneration, candidate["index_name"])
                record.state = "deleted"
                record.deleted_at = record.deleted_at or utc_now()
                db.commit()
            result["failed_index"] = None
        return result
    except Exception as exc:
        db.rollback()
        result["status"] = "failed"
        result["error"] = type(exc).__name__
        result["detail"] = str(exc) if isinstance(exc, RetentionBlocked) else (
            "Cleanup stopped; inspect index identities and the audit before retrying. "
            "The last deletion or audit commit may have an ambiguous outcome."
        )
        raise GenerationCleanupError(result) from exc
