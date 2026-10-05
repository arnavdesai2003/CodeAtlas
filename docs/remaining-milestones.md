# Remaining work and completion evidence

Checkpoint: 2026-10-05. This is an inventory of unresolved work, not a claim that
research questions or live verification have been completed.
All 431 offline tests passed in the final full run; actual evaluator help passed.

## Evaluation tooling

Completed: repository-aware matching, ground-truth validation, complete result
validation, metric arithmetic regressions, safe argument handling, sanitized
failure exits, and an explicit hybrid Recall@10 quality gate. Legacy evaluation
and tuning reject incomplete cases before retrieval; the multi-repository
evaluator reports valid subsets but exits nonzero for incomplete ground truth.

For the documented optimization baseline, run:

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 EMBEDDING_DEVICE=cpu TORCH_NUM_THREADS=1 .venv/bin/python -B -m scripts.evaluate_multirepo --minimum-hybrid-recall-at-10 0.880
```

Without the threshold, exit 0 means complete evaluation, not sufficient quality.
The gate uses unrounded hybrid Recall@10 and accepts equality. A passed gate
does not assess other recall cutoffs, MRR, semantic/reranked regressions, or
latency; compare the entire results with the documented baseline. Thresholds
never change retrieval or settings.

## Live verification dependency

The last completed live quality checkpoint was 2026-10-04. Docker Desktop was
manually paused during the subsequent evaluation attempt. Its CLI has no
unpause command in the installed version; restarting the entire engine is not
a substitute for read-only verification. Resume Docker through its Dashboard
before running the command above. No new live quality or capacity is claimed.
See [validation history](validation-checkpoint-2026-10-04.md).

## Open research and architectural work

| Work | Evidence and prerequisite | Completion criterion |
| --- | --- | --- |
| Forwarding-delay attribution | Host-forwarded curl reproduces the penalty without Python; privileged packet capture was unavailable. | Matched packet/server/forwarding traces establish a mechanism, followed by matched HTTP and quality checks for any fix. Keep transport defaults unchanged until justified. |
| Cross-process miss sharing | Mixed-query two-worker bursts vary with assignment; current coalescing is intentionally process-local. | A representative workload establishes a material need, then a bounded failure/recovery design and generation-fencing tests demonstrate safe sharing. No distributed lock or infrastructure is justified yet. |
| Atomic metadata/cache/index visibility | Full alias publication is atomic in Elasticsearch, while incremental sync updates in place and metadata/cache transitions are separate. | Specify reader and writer visibility during each failure window, then implement a recoverable protocol with crash/interleaving and real-store verification. Current journals and retained generations alone do not provide cross-store atomicity. |
| Rollback and reader-safe online retention | Retention requires quiescence; retained indices lack coordinated metadata/cache rollback and reader leases. | An explicit rollback/lease design and failure protocol is reviewed and verified before removing maintenance quiescence or offering rollback. No automatic deletion is enabled. |

These items have no agreed implementation milestone or unconditional performance
target. Existing recovery and retention tools remain the operator workflow;
do not delete journals, manually change aliases, or run full indexing alongside
legacy writers to force progress. Relevant evidence is in
[performance](performance.md), [coalescing](cache-coalescing.md),
[publication](atomic-publication.md), and [retention](index-generation-retention.md).
