"""Run the CodeAtlas local release checks; --live adds isolated store checks."""
import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OFFLINE = [("offline_tests", ["-m", "unittest", "discover", "-s", "tests", "-v"])]
LIVE = [
    ("recovery_journals", ["-m", "scripts.inspect_recovery_jobs"]),
    ("initial_inventory", ["-m", "scripts.manage_index_generations", "inspect"]),
    ("quality_gate", ["-m", "scripts.evaluate_multirepo", "--minimum-hybrid-recall-at-10", "0.880"]),
    ("api_smoke", ["-m", "scripts.verify_api_smoke"]),
    ("redis_protocol", ["-m", "scripts.verify_cache_protocol"]),
    ("publication_protocol", ["-m", "scripts.verify_publication_protocol"]),
    ("writer_locks", ["-m", "scripts.verify_writer_locks"]),
    ("writer_processes", ["-m", "scripts.verify_writer_processes"]),
    ("http_receipt", ["-m", "scripts.verify_http_receipt"]),
    ("final_recovery_journals", ["-m", "scripts.inspect_recovery_jobs"]),
    ("final_inventory", ["-m", "scripts.manage_index_generations", "inspect"]),
]


def run_check(arguments, env, timeout):
    process = subprocess.Popen([sys.executable, "-B", *arguments], cwd=ROOT,
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate(timeout=5)
        return -signal.SIGTERM, output
    except KeyboardInterrupt:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        raise
    return process.returncode, output


def verify(*, live, timeout):
    outcomes = []
    identity = None
    for name, arguments in OFFLINE + (LIVE if live else []):
        env = os.environ.copy()
        if name == "offline_tests":
            # Unit fixtures must not inherit a deployed API's authentication
            # or real store credentials. Live checks get a fresh copy below.
            env.update(APP_ENV="test", API_KEY="", GITHUB_WEBHOOK_SECRET="",
                       ELASTICSEARCH_API_KEY="",
                       DATABASE_URL="postgresql+psycopg://test:test@offline.invalid/test",
                       ELASTICSEARCH_URL="http://offline.invalid:9200",
                       REDIS_URL="redis://offline.invalid:6379/15",
                       HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        else:
            env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                       EMBEDDING_DEVICE="cpu", TORCH_NUM_THREADS="1")
        print(json.dumps({"check": name, "status": "running"}), flush=True)
        code, output = run_check(arguments, env, timeout)
        if code != 0:
            # Preserve only known scratch identifiers for operator reconciliation.
            for line in output.splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("status") == "starting":
                    scratch = {field: row[field] for field in (
                        "private_namespace", "index_prefix", "alias", "redis_prefix") if field in row}
                    if scratch:
                        print(json.dumps({"check": name, "scratch_to_inspect": scratch}), flush=True)
            raise RuntimeError(f"Check failed: {name}")
        if "recovery_journals" in name:
            report = json.loads(output)
            if (report.get("status") != "observed" or report.get("read_only") is not True
                    or any(report.get(field) != [] for field in (
                        "sync_jobs", "full_index_jobs", "publication_jobs",
                        "conflicting_sync_and_full_repository_ids",
                        "conflicting_publication_and_sync_repository_ids"))):
                raise RuntimeError("Pending or malformed recovery state")
        if name in {"initial_inventory", "final_inventory"}:
            report = json.loads(output)
            active = report.get("active_index")
            entries = [row for row in report.get("indices", []) if row.get("index_name") == active]
            if report.get("issues") != [] or not active or len(entries) != 1:
                raise RuntimeError("Incomplete or problematic routing inventory")
            row = entries[0]
            current = (report.get("cluster_uuid"), active, row.get("index_uuid"), row.get("documents"))
            if (not current[0] or not current[2] or row.get("exists") is not True
                    or type(current[3]) is not int or current[3] < 1):
                raise RuntimeError("Invalid active generation identity")
            if identity is not None and identity != current:
                raise RuntimeError("Active corpus changed during release verification")
            identity = current
        outcomes.append(name)
        print(json.dumps({"check": name, "status": "passed"}), flush=True)
    return {"status": "passed", "scope": "local_release" if live else "offline_only",
            "checks": outcomes, "research_completed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use live stores and isolated scratch artifacts; no corpus rebuild.")
    parser.add_argument("--timeout", type=int, default=300, help="Per-check timeout in seconds (minimum 60).")
    args = parser.parse_args(argv)
    if args.timeout < 60:
        parser.error("timeout must be at least 60 seconds")
    try:
        report = verify(live=args.live, timeout=args.timeout)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__,
                          "detail": "Release verification incomplete; no success claimed."}))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
