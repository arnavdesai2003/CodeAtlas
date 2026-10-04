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
