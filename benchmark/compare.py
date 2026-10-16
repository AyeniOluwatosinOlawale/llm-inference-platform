"""
Compare lab2 benchmark results against live production platform results.

Usage:
    python benchmark/compare.py \
        --lab /path/to/inference-optimizer-lab2/results/headtohead_full/*.json \
        --prod production_benchmark_20261016_120000.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_lab_results(paths: list[str]) -> dict:
    """Load the best result file from lab2 results/ directory."""
    records: list[dict] = []
    for p in paths:
        try:
            data = json.loads(Path(p).read_text())
            # lab2 result files have a 'results' list of RequestRecord dicts
            if isinstance(data, list):
                records.extend(data)
            elif "results" in data:
                records.extend(data["results"])
        except Exception as e:
            print(f"Warning: could not load {p}: {e}")
    return records


def load_prod_results(path: str) -> dict:
    return json.loads(Path(path).read_text())


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(len(s) * p)
    return s[min(idx, len(s) - 1)]


def summarise_lab(records: list[dict], engine: str, model_filter: str) -> dict:
    """Extract p50/p95 TTFT and mean TPOT from lab2 RequestRecord list."""
    filtered = [r for r in records
                if r.get("engine_name", r.get("engine", "")) == engine
                and (not model_filter or model_filter in str(r.get("model", "")))]
    ttfts = [r["ttft_ms"] for r in filtered if r.get("ttft_ms") and not r.get("error")]
    tpots = [r["tpot_ms"] for r in filtered if r.get("tpot_ms") and not r.get("error")]
    return {
        "n": len(ttfts),
        "ttft_p50_ms": percentile(ttfts, 0.50),
        "ttft_p95_ms": percentile(ttfts, 0.95),
        "tpot_mean_ms": sum(tpots) / len(tpots) if tpots else 0,
    }


def print_comparison(lab_records: list[dict], prod_data: dict) -> None:
    prod_results = prod_data.get("results", {})

    print("\n╔══════════════════════ Lab2 vs Production Platform ══════════════════════╗")
    print(f"  Lab2 records loaded: {len(lab_records)}")
    print(f"  Production timestamp: {prod_data.get('metadata', {}).get('timestamp', 'unknown')}")
    print("╠" + "═" * 73 + "╣")
    print(f"  {'Source':<28} {'TTFT p50':>10} {'TTFT p95':>10} {'TPOT mean':>10} {'N':>5}")
    print("╠" + "═" * 73 + "╣")

    for engine in ["vllm", "sglang"]:
        for model in ["llama3", "qwen3"]:
            # Lab2 data (single-GPU, controlled environment)
            lab = summarise_lab(lab_records, engine, model)
            prod_key = f"{engine}/{model}"
            prod_summaries = prod_results.get(prod_key, [])

            # Use c=1 or the lowest concurrency production result for fair comparison
            prod = next(
                (s for s in sorted(prod_summaries, key=lambda x: x["concurrency"]) if s["n"] > 0),
                None
            )

            label = f"  lab2   {engine}/{model}"
            print(f"{label:<30} {lab['ttft_p50_ms']:>8.0f}ms {lab['ttft_p95_ms']:>8.0f}ms "
                  f"{lab['tpot_mean_ms']:>8.1f}ms {lab['n']:>5}")

            if prod:
                label2 = f"  prod   {engine}/{model} (c={prod['concurrency']})"
                delta_ttft = ""
                if lab["ttft_p50_ms"] > 0:
                    pct = (prod["ttft_p50_ms"] - lab["ttft_p50_ms"]) / lab["ttft_p50_ms"] * 100
                    delta_ttft = f"  [{pct:+.0f}%]"
                print(f"{label2:<30} {prod['ttft_p50_ms']:>8.0f}ms {prod['ttft_p95_ms']:>8.0f}ms "
                      f"{prod['tpot_mean_ms']:>8.1f}ms {prod['n']:>5}{delta_ttft}")
            else:
                print(f"  prod   {engine}/{model:<18} {'—':>10} {'—':>10} {'—':>10} {'—':>5}")

            print("  " + "─" * 71)

    print("╚" + "═" * 73 + "╝")
    print("  Positive % = production is slower than lab (expected at higher concurrency).")
    print("  Negative % = production is faster (caching, batching effects).")


def main():
    p = argparse.ArgumentParser(description="Compare lab2 vs production benchmark results")
    p.add_argument("--lab",  nargs="+", required=True, help="Path(s) to lab2 result JSON files")
    p.add_argument("--prod", required=True, help="Path to production_runner.py output JSON")
    args = p.parse_args()

    lab_records = load_lab_results(args.lab)
    prod_data = load_prod_results(args.prod)
    print_comparison(lab_records, prod_data)


if __name__ == "__main__":
    main()
