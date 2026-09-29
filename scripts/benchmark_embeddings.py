"""Isolated embedding diagnostic; run each configuration in a fresh process.

Uses the concurrent search workload but does not exercise HTTP or Elasticsearch.
Model loading/warm-up and numerical comparisons are outside measured intervals.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import statistics
from threading import Semaphore
from time import perf_counter

import numpy as np
from sentence_transformers import SentenceTransformer
import torch

from app.search.embeddings import MODEL_NAME
from scripts.benchmark_api import CONCURRENCY_LEVELS, QUERIES, TOTAL_REQUESTS, percentile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=["cpu", "mps"], required=True)
    parser.add_argument("--torch-threads", type=int)
    parser.add_argument("--max-inflight", type=int, default=0)
    args = parser.parse_args()
    if args.torch_threads is not None and args.torch_threads < 1:
        parser.error("--torch-threads must be positive")
    if args.max_inflight < 0:
        parser.error("--max-inflight must be nonnegative")
    if args.torch_threads is not None:
        torch.set_num_threads(args.torch_threads)
    model = SentenceTransformer(MODEL_NAME, device=args.device)
    gate = Semaphore(args.max_inflight) if args.max_inflight else nullcontext()
    references = [model.encode(q, normalize_embeddings=True, show_progress_bar=False) for q in QUERIES]
    print(f"Device={model.device}, torch threads={torch.get_num_threads()}, "
          f"max inflight={args.max_inflight or 'unbounded'}", flush=True)
    print("EMBEDDING ONLY; includes semaphore wait, excludes executor queue wait.")

    def request(index):
        start = perf_counter()
        with gate:
            vector = model.encode(QUERIES[index % len(QUERIES)],
                                  normalize_embeddings=True, show_progress_bar=False)
            # Match the application's conversion, including accelerator transfer.
            vector = vector.tolist()
        return (perf_counter() - start) * 1000, vector

    for workers in CONCURRENCY_LEVELS:
        start = perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(request, range(TOTAL_REQUESTS)))
        elapsed = perf_counter() - start
        latencies = [result[0] for result in results]
        vectors = np.asarray([vector for _, vector in results])
        if not np.isfinite(vectors).all():
            raise RuntimeError("Non-finite concurrent embeddings")
        max_error = max(float(np.max(np.abs(vector - references[i % len(QUERIES)])))
                        for i, vector in enumerate(vectors))
        if not np.isfinite(max_error) or max_error > 1e-4:
            raise RuntimeError(f"Concurrent embedding differs from serial reference: {max_error}")
        print(f"Workers={workers:2} requests={TOTAL_REQUESTS} req/s={TOTAL_REQUESTS / elapsed:.2f} "
              f"avg={statistics.mean(latencies):.3f} ms p95={percentile(latencies, .95):.3f} ms "
              f"max_abs_error={max_error:.2g}", flush=True)


if __name__ == "__main__":
    main()
