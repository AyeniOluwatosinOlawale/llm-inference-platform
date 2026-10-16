"""
Production benchmark runner — bridges inference-optimizer-lab2 against the live platform.

Reuses the VLLMClient and SGLangClient from lab2 directly.
Both engines expose the same OpenAI-compatible /v1/chat/completions endpoint,
so no adapter layer is needed: point base_url at the live service and run.

Usage:
    # Set env vars to point at the live cluster
    export VLLM_LLAMA3_URL=http://vllm.example.com
    export SGLANG_LLAMA3_URL=http://sglang.example.com
    export LITELLM_URL=http://vllm.example.com         # unified gateway
    export LITELLM_MASTER_KEY=sk-...

    # Install lab2 alongside this repo
    pip install -e /path/to/inference-optimizer-lab2

    python benchmark/production_runner.py --engine vllm --model llama3 --concurrency 1 8 32
    python benchmark/production_runner.py --engine sglang --model qwen3 --quick
    python benchmark/production_runner.py --compare  # run both, produce comparison
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

# Lab2 engine clients — call the live vLLM / SGLang HTTP endpoints.
# Install lab2: pip install -e /path/to/inference-optimizer-lab2
try:
    from inference_optimizer.engines.vllm import VLLMClient
    from inference_optimizer.engines.sglang import SGLangClient
    from inference_optimizer.sweep.config import SweepConfig
    from inference_optimizer.types import RequestRecord
except ImportError:
    raise ImportError(
        "inference-optimizer-lab2 not installed.\n"
        "Run: pip install -e /path/to/inference-optimizer-lab2\n"
        "or: pip install git+https://github.com/AyeniOluwatosinOlawale/inference-optimizer-lab2"
    )

# ── Configuration ──────────────────────────────────────────────────────────────

ENDPOINTS = {
    "vllm": {
        "llama3": os.environ.get("VLLM_LLAMA3_URL", "http://localhost:8000"),
        "qwen3":  os.environ.get("VLLM_QWEN3_URL",  "http://localhost:8001"),
    },
    "sglang": {
        "llama3": os.environ.get("SGLANG_LLAMA3_URL", "http://localhost:30000"),
        "qwen3":  os.environ.get("SGLANG_QWEN3_URL",  "http://localhost:30001"),
    },
}

# Model name strings as served by each engine (--served-model-name in deployment.yaml).
SERVED_MODEL_NAMES = {
    "vllm":   {"llama3": "llama3",        "qwen3": "qwen3"},
    "sglang": {"llama3": "llama3-sglang", "qwen3": "qwen3-sglang"},
}

DEFAULT_PROMPTS = [
    "Explain the difference between TTFT and TPOT in LLM inference.",
    "What is KV-cache and why does it matter for serving efficiency?",
    "Compare continuous batching in vLLM versus RadixAttention in SGLang.",
    "How does tensor parallelism affect inference latency at low concurrency?",
    "Describe prefix caching and when it provides the largest speedup.",
]


# ── Result types ───────────────────────────────────────────────────────────────

@dataclass
class BenchmarkPoint:
    engine: str
    model: str
    concurrency: int
    prompt_idx: int
    ttft_ms: float
    tpot_ms: float
    e2e_ms: float
    output_tokens: int
    error: str | None


@dataclass
class BenchmarkSummary:
    engine: str
    model: str
    concurrency: int
    n: int
    ttft_p50_ms: float
    ttft_p95_ms: float
    tpot_mean_ms: float
    throughput_req_s: float
    errors: int


# ── Core runner ────────────────────────────────────────────────────────────────

async def run_concurrency_level(
    client,
    model_name: str,
    concurrency: int,
    prompts: list[str],
    max_tokens: int = 256,
) -> list[BenchmarkPoint]:
    """Fire `concurrency` requests simultaneously, return per-request metrics."""
    engine = client.engine_name

    async def single(prompt: str, idx: int) -> BenchmarkPoint:
        try:
            record: RequestRecord = await client.stream_request(
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=0.0,
                concurrency=concurrency,
            )
            return BenchmarkPoint(
                engine=engine,
                model=model_name,
                concurrency=concurrency,
                prompt_idx=idx,
                ttft_ms=record.ttft_ms if record.ttft_ms else 0.0,
                tpot_ms=record.tpot_ms if record.tpot_ms else 0.0,
                e2e_ms=record.e2e_ms,
                output_tokens=record.output_tokens,
                error=record.error,
            )
        except Exception as exc:
            return BenchmarkPoint(
                engine=engine, model=model_name, concurrency=concurrency,
                prompt_idx=idx, ttft_ms=0, tpot_ms=0, e2e_ms=0,
                output_tokens=0, error=str(exc),
            )

    tasks = [single(prompts[i % len(prompts)], i) for i in range(concurrency)]
    t0 = time.perf_counter()
    results = await asyncio.gather(*tasks)
    wall_ms = (time.perf_counter() - t0) * 1000

    return list(results)


def summarise(points: list[BenchmarkPoint]) -> BenchmarkSummary:
    good = [p for p in points if p.error is None]
    if not good:
        return BenchmarkSummary(
            engine=points[0].engine, model=points[0].model,
            concurrency=points[0].concurrency, n=0,
            ttft_p50_ms=0, ttft_p95_ms=0, tpot_mean_ms=0,
            throughput_req_s=0, errors=len(points),
        )

    ttfts = sorted(p.ttft_ms for p in good)
    tpots = [p.tpot_ms for p in good if p.tpot_ms > 0]
    wall = sum(p.e2e_ms for p in good) / len(good) / 1000  # avg seconds

    n = len(ttfts)
    return BenchmarkSummary(
        engine=good[0].engine,
        model=good[0].model,
        concurrency=good[0].concurrency,
        n=n,
        ttft_p50_ms=ttfts[n // 2],
        ttft_p95_ms=ttfts[int(n * 0.95)],
        tpot_mean_ms=sum(tpots) / len(tpots) if tpots else 0,
        throughput_req_s=n / wall if wall > 0 else 0,
        errors=len(points) - n,
    )


async def benchmark_engine(
    engine: str,
    model: str,
    concurrency_levels: list[int],
    prompts: list[str],
) -> list[BenchmarkSummary]:
    base_url = ENDPOINTS[engine][model]
    served_name = SERVED_MODEL_NAMES[engine][model]

    if engine == "vllm":
        client = VLLMClient(base_url=base_url, model=served_name)
    else:
        client = SGLangClient(base_url=base_url, model=served_name)

    summaries = []
    for c in concurrency_levels:
        print(f"  [{engine}/{model}] concurrency={c} ...", end=" ", flush=True)
        points = await run_concurrency_level(client, served_name, c, prompts)
        s = summarise(points)
        print(f"TTFT p50={s.ttft_p50_ms:.0f}ms  p95={s.ttft_p95_ms:.0f}ms  "
              f"TPOT={s.tpot_mean_ms:.1f}ms  {s.throughput_req_s:.1f} req/s  errors={s.errors}")
        summaries.append(s)

    return summaries


# ── CLI ────────────────────────────────────────────────────────────────────────

async def main_async(args) -> None:
    concurrency = args.concurrency or [1, 4, 8, 16, 32]
    prompts = DEFAULT_PROMPTS

    results: dict[str, list[BenchmarkSummary]] = {}

    pairs = []
    if args.compare:
        pairs = [
            ("vllm",   "llama3"), ("sglang", "llama3"),
            ("vllm",   "qwen3"),  ("sglang", "qwen3"),
        ]
    else:
        pairs = [(args.engine, args.model)]

    if args.quick:
        concurrency = [1, 8, 32]

    for engine, model in pairs:
        print(f"\n==> {engine.upper()} / {model} ...")
        key = f"{engine}/{model}"
        results[key] = await benchmark_engine(engine, model, concurrency, prompts)

    # Write output
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.output or f"production_benchmark_{ts}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": {
            "timestamp": ts,
            "concurrency_levels": concurrency,
            "source": "production_runner.py",
            "platform": "llm-inference-platform",
        },
        "results": {k: [asdict(s) for s in v] for k, v in results.items()},
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"\n==> Results written to {out_path}")

    # Print comparison table if --compare
    if args.compare and len(results) >= 2:
        print("\n┌─────────────────── Live Engine Comparison ───────────────────────┐")
        print(f"{'Engine/Model':<22} {'c':>4}  {'TTFT p50':>10} {'TTFT p95':>10} {'TPOT':>8} {'Req/s':>7}")
        print("├" + "─" * 67 + "┤")
        for key, summaries in results.items():
            for s in summaries:
                print(f"  {key:<20} {s.concurrency:>4}  "
                      f"{s.ttft_p50_ms:>8.0f}ms {s.ttft_p95_ms:>8.0f}ms "
                      f"{s.tpot_mean_ms:>6.1f}ms {s.throughput_req_s:>6.1f}")
        print("└" + "─" * 67 + "┘")


def main():
    p = argparse.ArgumentParser(description="Production benchmark runner for llm-inference-platform")
    p.add_argument("--engine", choices=["vllm", "sglang"], default="vllm")
    p.add_argument("--model",  choices=["llama3", "qwen3"], default="llama3")
    p.add_argument("--concurrency", type=int, nargs="+")
    p.add_argument("--quick", action="store_true", help="Run c=[1,8,32] only")
    p.add_argument("--compare", action="store_true", help="Run all engine/model combos and compare")
    p.add_argument("--output", type=str, help="Output JSON path")
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
