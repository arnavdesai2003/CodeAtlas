"""Inspect generations, save a reviewable plan, or apply it during maintenance."""
import argparse
import json
from pathlib import Path

from app.core.clients import elasticsearch_client
from app.db.database import SessionLocal
from app.search.generation_retention import (
    GenerationCleanupError, RetentionBlocked, RetentionPolicy, apply_cleanup_plan,
    inspect_generations, make_cleanup_plan,
)


def _unique_plan_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RetentionBlocked("Cleanup plan contains duplicate JSON fields; create a new unambiguous plan.")
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "plan"):
        command = commands.add_parser(name)
        command.add_argument("--min-age-hours", type=float, default=24)
        command.add_argument("--keep-retired", type=int, default=2)
        if name == "plan":
            command.add_argument("--output", type=Path, required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--quiesced", action="store_true", help="Attest that all API/readers/writers are stopped.")
    args = parser.parse_args(argv)
    db = SessionLocal()
    try:
        client = elasticsearch_client.options(request_timeout=60)
        if args.command == "apply":
            plan = json.loads(args.plan.read_text(encoding="utf-8"), object_pairs_hook=_unique_plan_object)
            result = apply_cleanup_plan(db, client, plan, quiesced=args.quiesced)
        else:
            report = inspect_generations(db, client, policy=RetentionPolicy(args.min_age_hours, args.keep_retired))
            result = report if args.command == "inspect" else make_cleanup_plan(report)
            if args.command == "plan":
                # Preserve existing reviewed plans; use a new output filename.
                with args.output.open("x", encoding="utf-8") as output:
                    json.dump(result, output, indent=2)
                    output.write("\n")
        print(json.dumps(result, indent=2))
        return 0
    except GenerationCleanupError as exc:
        print(json.dumps(exc.result, indent=2))
        return 1
    except RetentionBlocked as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__, "detail": str(exc)}))
        return 1
    except Exception as exc:
        # Service exceptions may contain connection details; report only type.
        print(json.dumps({"status": "failed", "error": type(exc).__name__,
                          "detail": "Check service health, schema setup and arguments before retrying."}))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
