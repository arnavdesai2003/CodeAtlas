# Live retrieval validation checkpoint — 2026-10-04

After the CLI/recovery hardening milestones, read-only local validation reproduced
every inherited retrieval metric. No retrieval, model, ranking or corpus change
was made for this checkpoint.

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 EMBEDDING_DEVICE=cpu TORCH_NUM_THREADS=1 .venv/bin/python -B -m scripts.evaluate_multirepo
.venv/bin/python -B -m scripts.inspect_recovery_jobs
```

Evaluation ran on the local macOS Apple Silicon development setup with cached
models, downloads disabled, CPU embeddings and one PyTorch thread. These are
process-only settings; `.env`, default device/thread settings and existing API
processes were not changed. The evaluator calls retrieval methods directly; it
does not measure HTTP, normal cache behavior, throughput or latency.

All 25 defined ground-truth cases validated against PostgreSQL; zero were excluded.

| Metric | BM25 | Semantic | Hybrid | Reranked |
| --- | ---: | ---: | ---: | ---: |
| Recall@1 | .080 | .440 | .360 | .400 |
| Recall@3 | .240 | .720 | .560 | .520 |
| Recall@5 | .280 | .760 | .760 | .600 |
| Recall@10 | .280 | .880 | .880 | .800 |
| MRR | .168 | .585 | .499 | .493 |

A read-only store snapshot found six repositories and 4,340 PostgreSQL symbols:
micrograd 40, click 1,992, requests 807, httpx 1,241, itsdangerous 144 and
markupsafe 116. Elasticsearch resolved the legacy physical `codeatlas_symbols`
index, UUID `RAHAzCX7Tn2804qrSkpkCQ`, with 4,340 documents. Metadata inspection
reported no pending sync, full-index or publication jobs and no journal conflicts.
These are new observations, not inherited counts; the store checks and evaluation
are separate read-only operations rather than one cross-store atomic snapshot.

No fetch/reset, ingestion, reindexing, publication, cache flush/rotation, schema
initialization, service/API restart or configuration write occurred. The previous
404-test offline suite result remains inherited and was not rerun for this
documentation-only checkpoint. Existing benchmark results remain historical;
this validation makes no new performance or capacity claim. The fixed 25-case
evaluation does not establish quality for all queries or concurrent publication.

## Evaluation exit correctness follow-up

The evaluation CLI now parses arguments before ground-truth access. Help exits 0;
unsupported arguments exit 2. Invalid ground-truth cases still appear in the report
and are excluded from subset metrics, but now cause exit 1. With zero valid cases,
the command skips all retrieval and produces no misleading zero-metric summary.
A complete valid set returns 0 after all four methods finish. This exit status
checks ground-truth completeness/execution, not a quality threshold: compare the
reported metrics against the baseline separately.

Four offline regressions bring the suite to 408 passing tests. A fresh read-only
CPU/one-thread evaluation with cached models/downloads disabled exited 0, validated
all 25 cases and again reproduced every metric in the table above. No new store
count/UUID snapshot was taken for this follow-up. No corpus/index/cache/schema/
settings/API changes or new performance/capacity claim occurred.

## Evaluation failure output follow-up — 2026-10-05

The CLI now catches ordinary runtime exceptions from evaluation, prints their
type with `Evaluation failed; partial output is not a complete result.`, and
returns exit 1 without raw exception messages or dependency tracebacks. Database
validation failure prevents retrieval; a failing retrieval method stops subsequent
methods without retry and before the final summary. Existing complete/incomplete
returns remain 0/1, and help/argument-error SystemExit values remain 0/2. Direct
Python `main()` calls still propagate exceptions for callers.

Three offline regressions cover database failure, each of the four method failure
positions, no retry/final summary and preserved exit statuses. All 411 offline
tests pass; an actual help invocation still exited safely. No live evaluation or
store inspection was rerun, so the quality/count results above remain the prior
2026-10-04 observations. No data/index/cache/settings/API changes or new quality/
performance claim occurred.
