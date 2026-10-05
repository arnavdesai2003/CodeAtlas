# Remaining work and completion evidence

Checkpoint: 2026-10-05. This is an inventory of unresolved work, not a claim that
research questions or live verification have been completed.
All 448 offline tests passed in the combined live release acceptance run; new
entrypoint help passed. All 12 release checks passed; see
[release acceptance](project-completion.md). The research items below remain open.

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

## Live verification completed after restart

After the user restarted Docker on 2026-10-05, all three stores were healthy.
The live multi-repository quality gate passed with all 25 cases valid and every
baseline metric reproduced. Legacy evaluation and its read-only weight sweep
also exited successfully. Isolated Redis/publication, PostgreSQL lock/process,
and real-socket HTTP checks passed with owned artifacts/processes cleaned up.
No performance/capacity measurement or recommendation application occurred.
See [restart validation](validation-restart-2026-10-05.md).

## Open research and architectural work

| Work | Evidence and prerequisite | Completion criterion |
| --- | --- | --- |
| Forwarding-delay attribution | Paired host/container traces show a long inter-request gap across forwarding but sub-ms container response after arrival. | Separate prior-response delivery from next-request forwarding and identify a mechanism, then matched HTTP/quality checks for any fix. Keep defaults unchanged until justified. |
| Cross-process miss sharing | Mixed-query two-worker bursts vary with assignment; current coalescing is intentionally process-local. | A representative workload establishes a material need, then a bounded failure/recovery design and generation-fencing tests demonstrate safe sharing. No distributed lock or infrastructure is justified yet. |
| Atomic metadata/cache/index visibility | Full alias publication is atomic in Elasticsearch, while incremental sync updates in place and metadata/cache transitions are separate. | Specify reader and writer visibility during each failure window, then implement a recoverable protocol with crash/interleaving and real-store verification. Current journals and retained generations alone do not provide cross-store atomicity. |
| Rollback and reader-safe online retention | Retention requires quiescence; retained indices lack coordinated metadata/cache rollback and reader leases. | An explicit rollback/lease design and failure protocol is reviewed and verified before removing maintenance quiescence or offering rollback. No automatic deletion is enabled. |

These items have no agreed implementation milestone or unconditional performance
target. Existing recovery and retention tools remain the operator workflow;
do not delete journals, manually change aliases, or run full indexing alongside
legacy writers to force progress. Relevant evidence is in
[performance](performance.md), [coalescing](cache-coalescing.md),
[publication](atomic-publication.md), and [retention](index-generation-retention.md).
The latest [header trace](forwarding-packet-trace.md) removes the earlier host
capture-access block without establishing a unique root cause.
The subsequent [paired trace](paired-forwarding-trace.md) localizes the dominant
gap across the forwarding boundary; exact leg/mechanism remains unresolved.
The [idle-time controls](transport-idle-controls.md) passed in both delay orders
after Docker resumed. Waiting reduces subsequent alias latency but can worsen
total pair time; no unique mechanism or production change is established.
