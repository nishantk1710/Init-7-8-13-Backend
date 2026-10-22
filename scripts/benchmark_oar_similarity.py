"""Benchmark the OAR donor-similarity engine on deterministic synthetic data.

    python -m scripts.benchmark_oar_similarity                       # 100,000 materials
    python -m scripts.benchmark_oar_similarity --materials 20000 --sample 500
    python -m scripts.benchmark_oar_similarity --legacy-root ../Init-7-8-13-Backend

What it measures, against the same synthetic catalogue:

* embedding generation: how many descriptions the model is asked to embed on a
  first run and on a repeat run (the repeat should be zero), and the hit rate;
* index construction time for the eligible-donor partitions;
* Stage A retrieval latency for EVERY target, and the shortlist sizes;
* end-to-end latency (retrieval + exact scoring + ranking + estimate) on a
  sample of targets, for structured-only and for hybrid retrieval;
* exhaustive scoring on a smaller sample, both the new exhaustive mode and --
  with ``--legacy-root`` -- the CURRENT production implementation imported
  from another checkout, so the baseline is the real code, not a stand-in;
* retrieval recall: how often the two-stage Top-10 equals the exhaustive one;
* precision@5 / @10 against the synthetic ground truth (same part family);
* peak resident memory.

WHAT IT DOES NOT MEASURE

Real matching accuracy. The catalogue is synthetic: every material belongs to a
"part family", descriptions are family templates, and the embedding provider is
a deterministic stand-in whose vectors cluster by family. Precision here shows
that the engine uses the signals it is given correctly; it does not show that
the real embedding model puts real VZI spare-part descriptions in the right places. That needs
the real model and expert-reviewed pairs (``oar.evaluation``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import numpy as np

DIMENSIONS = 384
CRITICALITIES = (["NORMAL"] * 60 + ["IMPACT"] * 15 + ["CRITICAL"] * 10 + ["INSURANCE"] * 5 + [None] * 10)
UOMS = ["EA"] * 80 + ["SET"] * 8 + ["M"] * 6 + ["KG"] * 4 + [None] * 2
NOUNS = ["bearing", "seal kit", "impeller", "gate valve", "motor", "gearbox", "liner",
         "filter element", "coupling", "pump casing", "screen panel", "hydraulic hose"]


# --- Synthetic catalogue -----------------------------------------------------------


def _family_text(family: int, variant: int) -> str:
    noun = NOUNS[family % len(NOUNS)]
    return f"{noun} type F{family:05d} spec {variant} {['std', 'heavy duty', 'OEM', 'alt'][variant % 4]}"


def generate(materials: int, donor_share: float, seed: int):
    """``(donors, targets, inventory, family_of)`` -- deterministic for a seed."""
    from app.initiatives.i7.oar.types import CandidateAttributes

    rng = random.Random(seed)
    families = max(50, materials // 25)
    groups = max(20, families // 10)
    family_group = [rng.randrange(groups) for _ in range(families)]
    family_price = [round(rng.lognormvariate(5, 1.2), 2) for _ in range(families)]
    family_uom = [rng.choice(UOMS) for _ in range(families)]
    weights = [1 / (i + 1) ** 0.6 for i in range(families)]  # a few families are common

    donor_count = int(materials * donor_share)
    donors, targets = [], []
    family_of: dict[tuple[str, str], int] = {}
    chosen = rng.choices(range(families), weights=weights, k=materials)
    for i, family in enumerate(chosen):
        donor = i < donor_count
        number = f"{'D' if donor else 'T'}{i:07d}"
        plant = rng.choice(["1300", "1500"])
        group = family_group[family] if rng.random() > 0.15 else rng.randrange(groups)
        attrs = CandidateAttributes(
            sap_material_number=number,
            sap_plant_code=plant,
            criticality=rng.choice(CRITICALITIES),
            is_active=rng.choices([True, False, None], weights=[95, 3, 2])[0],
            history_months=(rng.choice([6, 9, 11] + list(range(12, 61))) if donor else rng.randrange(0, 6)),
            material_group=f"G{group:04d}" if rng.random() > 0.03 else None,
            equipment_type=None,
            base_unit_of_measure=family_uom[family],
            circuit=None,
            manufacturer=None,  # the CSV extract carries no MFRNR
            unit_price=(Decimal(str(round(family_price[family] * rng.uniform(0.7, 1.3), 2)))
                        if rng.random() > 0.1 else None),
            description=_family_text(family, rng.randrange(5)) if rng.random() > 0.05 else None,
        )
        family_of[(number, plant)] = family
        (donors if donor else targets).append(attrs)

    inventory = {
        (d.sap_material_number, d.sap_plant_code): {
            "safety_stock": rng.randint(0, 40), "rop": rng.randint(1, 80), "max_stock": rng.randint(10, 200),
        }
        for d in donors if rng.random() < 0.6
    }
    return donors, targets, inventory, family_of


class SyntheticProvider:
    """Deterministic family-clustered vectors, counting what it embeds. Accepts
    both the new and the legacy provider protocol."""

    model_name = "synthetic-minilm-stand-in"
    model_version = "bench-1"
    is_available = True

    def __init__(self) -> None:
        self.calls = 0
        self.texts = 0
        self._bases: dict[int, np.ndarray] = {}

    def _base(self, family: int) -> np.ndarray:
        if family not in self._bases:
            self._bases[family] = np.random.default_rng(family).normal(size=DIMENSIONS)
        return self._bases[family]

    def vector(self, text: str) -> np.ndarray:
        family = int(text.split(" F", 1)[1][:5]) if " F" in text else 0
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:12], 16)
        noise = np.random.default_rng(seed).normal(size=DIMENSIONS)
        return (self._base(family) + 0.45 * noise).astype(np.float32)

    def encode(self, texts):
        self.calls += 1
        self.texts += len(texts)
        return [self.vector(t) for t in texts]


class LegacyListProvider(SyntheticProvider):
    """The legacy engine computes cosine in pure Python over lists, and keeps
    its own per-instance cache keyed by description."""

    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[str, list[float]] = {}

    def encode(self, texts):
        out = []
        for t in texts:
            if t not in self._cache:
                self.texts += 1
                self._cache[t] = [float(x) for x in self.vector(t)]
            out.append(self._cache[t])
        return out


def peak_memory_mb() -> float | None:
    """Peak resident memory of this process, in MB."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL("psapi")
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
            return counters.PeakWorkingSetSize / 2**20
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return None


def precision(results_neighbours: dict, family_of: dict, k: int) -> float | None:
    """Mean share of each target's top-k neighbours from the target's own family."""
    values = []
    for target_key, neighbour_keys in results_neighbours.items():
        top = neighbour_keys[:k]
        if not top:
            continue
        values.append(sum(1 for n in top if family_of.get(n) == family_of[target_key]) / len(top))
    return round(sum(values) / len(values), 4) if values else None


def policy():
    from app.initiatives.i7.policy import PolicyDocument
    from app.initiatives.i7.policy.dev_fixtures import (
        load_mock_max_stock_policy,
        load_mock_service_level_policy,
    )

    return PolicyDocument(
        service_level=load_mock_service_level_policy(), max_stock=load_mock_max_stock_policy()
    )


# --- The legacy engine, run in its own process --------------------------------------


def run_legacy(args) -> dict:
    """Exhaustive scoring with the code at ``--legacy-root`` (current production)."""
    from app.initiatives.i7.oar import repository, service

    donors, targets, inventory, _ = generate(args.materials, args.donor_share, args.seed)
    sample = _sample(targets, args)[: args.legacy_sample]
    provider = LegacyListProvider()
    price_span = repository.price_range(donors)
    pol = policy()
    neighbours, started = {}, time.perf_counter()
    for target in sample:
        result = service.evaluate_target(target, donors, provider, price_span, inventory, pol)
        neighbours[f"{target.sap_material_number}|{target.sap_plant_code}"] = [
            f"{n.material}|{n.plant}" for n in result.neighbours
        ]
    elapsed = time.perf_counter() - started
    return {
        "targets": len(sample),
        "seconds": elapsed,
        "ms_per_target": 1000 * elapsed / max(1, len(sample)),
        "neighbours": neighbours,
        "peak_memory_mb": peak_memory_mb(),
    }


# --- The new engine -------------------------------------------------------------------


def run_new(args) -> dict:
    from app.initiatives.i7.oar import engine, repository, retrieval, service
    from app.initiatives.i7.oar.text_similarity import (
        EmbeddingCache,
        InMemoryEmbeddingStore,
        PrecomputedEmbeddingProvider,
    )

    report: dict = {"config": vars(args).copy()}
    started = time.perf_counter()
    donors, targets, inventory, family_of = generate(args.materials, args.donor_share, args.seed)
    report["catalogue"] = {
        "materials": len(donors) + len(targets), "donors": len(donors), "targets": len(targets),
        "generate_seconds": round(time.perf_counter() - started, 2),
    }
    pol = policy()
    price_span = repository.price_range(donors)

    # Embeddings: first run, then a repeat run over the same store.
    store = InMemoryEmbeddingStore()
    provider = SyntheticProvider()
    descriptions = [t.description for t in targets] + [d.description for d in donors]
    t0 = time.perf_counter()
    first = EmbeddingCache(provider, store=store, batch_size=256)
    embeddings = first.ensure(descriptions)
    first_seconds = time.perf_counter() - t0
    repeat_provider = SyntheticProvider()
    repeat = EmbeddingCache(repeat_provider, store=store, batch_size=256)
    t0 = time.perf_counter()
    repeat.ensure(descriptions)
    report["embeddings"] = {
        "descriptions": len(descriptions),
        "unique": first.stats.unique,
        "first_run_generated": first.stats.generated,
        "first_run_seconds": round(first_seconds, 2),
        "repeat_run_generated": repeat.stats.generated,
        "repeat_run_hit_rate": repeat.stats.hit_rate,
        "repeat_run_seconds": round(time.perf_counter() - t0, 2),
    }

    config = retrieval.RetrievalConfig(
        structured_pool=args.structured_pool, semantic_pool=args.semantic_pool,
        fused_pool=args.fused_pool, exhaustive_below=args.exhaustive_below,
    )

    # Index construction.
    t0 = time.perf_counter()
    hybrid_index = engine.build_index(donors, price_range=price_span, policy=pol, embeddings=embeddings)
    report["index"] = {
        "seconds": round(time.perf_counter() - t0, 2),
        "partitions": {k: len(v) for k, v in hybrid_index.partitions.items()},
    }

    # Stage A over EVERY target.
    t0 = time.perf_counter()
    sizes = []
    for start in range(0, len(targets), config.target_batch_size):
        batch = targets[start : start + config.target_batch_size]
        encodings = [hybrid_index.encode_target(t) for t in batch]
        rows = [None] * len(batch)
        groups: dict = {}
        for i, t in enumerate(batch):
            if t.criticality in hybrid_index.partitions:
                groups.setdefault(t.criticality, []).append(i)
        for crit, positions in groups.items():
            block = retrieval.cosine_block(hybrid_index.partitions[crit], [encodings[i].vector for i in positions])
            for i, row in zip(positions, block):
                rows[i] = row
        for i, t in enumerate(batch):
            partition = hybrid_index.partition_for(t)
            if partition is None:
                continue
            sizes.append(len(hybrid_index.shortlist(partition, t, encodings[i], rows[i], config).indices))
    stage_a = time.perf_counter() - t0
    report["stage_a_all_targets"] = {
        "targets": len(targets),
        "seconds": round(stage_a, 2),
        "ms_per_target": round(1000 * stage_a / len(targets), 3),
        "shortlist_mean": round(float(np.mean(sizes)), 1) if sizes else 0,
        "shortlist_max": int(max(sizes)) if sizes else 0,
    }

    sample = _sample(targets, args)
    exhaustive_sample = sample[: args.exhaustive_sample]

    def end_to_end(name, emb, index, chosen):
        stats = engine.EngineStats()
        t0 = time.perf_counter()
        results = list(
            engine.evaluate_targets(
                chosen, donors, provider=PrecomputedEmbeddingProvider(emb), embeddings=emb,
                price_range=price_span, inventory_values=inventory, policy=pol,
                config=config, index=index, stats=stats,
            )
        )
        elapsed = time.perf_counter() - t0
        neighbours = {
            (r.sap_material_number, r.sap_plant_code): [(n.material, n.plant) for n in r.neighbours]
            for r in results
        }
        report[name] = {
            "targets": len(chosen),
            "seconds": round(elapsed, 2),
            "ms_per_target": round(1000 * elapsed / max(1, len(chosen)), 2),
            "exact_scores_share_of_eligible_pairs": round(stats.scored_fraction, 4),
            "precision_at_5_synthetic": precision(neighbours, family_of, 5),
            "precision_at_10_synthetic": precision(neighbours, family_of, 10),
            "targets_with_5_plus_neighbours": sum(1 for r in results if len(r.neighbours) >= 5),
            "donor_eligibility_rate": _donor_rate(results),
        }
        return results

    structured_index = engine.build_index(donors, price_range=price_span, policy=pol, embeddings=None)
    end_to_end("two_stage_structured_only", None, structured_index, sample)
    hybrid = end_to_end("two_stage_hybrid", embeddings, hybrid_index, sample)

    # The new exhaustive mode on a smaller sample: the reference for recall.
    t0 = time.perf_counter()
    scoring_provider = PrecomputedEmbeddingProvider(embeddings)
    exhaustive = [
        service.evaluate_target(t, donors, scoring_provider, price_span, inventory, pol)
        for t in exhaustive_sample
    ]
    elapsed = time.perf_counter() - t0
    hybrid_by_key = {(r.sap_material_number, r.sap_plant_code): r for r in hybrid}
    same_top10 = sum(
        1 for r in exhaustive
        if [(n.material, n.plant, n.score.combined) for n in r.neighbours]
        == [(n.material, n.plant, n.score.combined)
            for n in hybrid_by_key[(r.sap_material_number, r.sap_plant_code)].neighbours]
    )
    expected = sum(len(r.neighbours) for r in exhaustive)
    found = sum(
        len({(n.material, n.plant) for n in r.neighbours}
            & {(n.material, n.plant) for n in hybrid_by_key[(r.sap_material_number, r.sap_plant_code)].neighbours})
        for r in exhaustive
    )
    report["exhaustive_new_engine"] = {
        "targets": len(exhaustive_sample),
        "seconds": round(elapsed, 2),
        "ms_per_target": round(1000 * elapsed / max(1, len(exhaustive_sample)), 2),
        "two_stage_identical_top10_share": round(same_top10 / max(1, len(exhaustive)), 4),
        "two_stage_recall_of_exhaustive_neighbours": round(found / expected, 4) if expected else None,
    }

    if args.legacy_root:
        legacy = _run_legacy_subprocess(args)
        legacy_neighbours = {
            tuple(k.split("|")): [tuple(n.split("|")) for n in v] for k, v in legacy["neighbours"].items()
        }
        report["legacy_exhaustive_current_production"] = {
            "targets": legacy["targets"],
            "seconds": round(legacy["seconds"], 2),
            "ms_per_target": round(legacy["ms_per_target"], 2),
            "precision_at_5_synthetic": precision(legacy_neighbours, family_of, 5),
            "precision_at_10_synthetic": precision(legacy_neighbours, family_of, 10),
            "peak_memory_mb": legacy["peak_memory_mb"],
        }
        same_targets = {
            key: [(n.material, n.plant) for n in hybrid_by_key[key].neighbours]
            for key in legacy_neighbours
        }
        report["two_stage_hybrid_on_legacy_targets"] = {
            "targets": len(same_targets),
            "precision_at_5_synthetic": precision(same_targets, family_of, 5),
            "precision_at_10_synthetic": precision(same_targets, family_of, 10),
        }

    report["peak_memory_mb"] = peak_memory_mb()
    return report


def _sample(targets, args):
    """The same targets in every process and every configuration: always drawn
    with k = --sample, then sliced, because random.sample's picks depend on k."""
    return random.Random(args.seed + 1).sample(targets, min(args.sample, len(targets)))


def _donor_rate(results):
    neighbours = [n for r in results for n in r.neighbours]
    return round(sum(1 for n in neighbours if n.inventory_eligible) / len(neighbours), 4) if neighbours else None


def _run_legacy_subprocess(args) -> dict:
    command = [
        sys.executable, "-m", "scripts.benchmark_oar_similarity", "--engine", "legacy",
        "--legacy-root", str(args.legacy_root), "--materials", str(args.materials),
        "--donor-share", str(args.donor_share), "--seed", str(args.seed),
        "--legacy-sample", str(args.legacy_sample), "--sample", str(args.sample),
    ]
    here = Path(__file__).resolve().parents[1]
    output = subprocess.run(command, capture_output=True, text=True, cwd=here, check=True)
    return json.loads(output.stdout.strip().splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--materials", type=int, default=100_000)
    parser.add_argument("--donor-share", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=20261012)
    parser.add_argument("--sample", type=int, default=2000, help="targets for end-to-end timing")
    parser.add_argument("--exhaustive-sample", type=int, default=40)
    parser.add_argument("--legacy-sample", type=int, default=20)
    parser.add_argument("--legacy-root", type=Path, default=None,
                        help="a checkout holding the current production OAR code, for the baseline")
    parser.add_argument("--structured-pool", type=int, default=50)
    parser.add_argument("--semantic-pool", type=int, default=50)
    parser.add_argument("--fused-pool", type=int, default=100)
    parser.add_argument("--exhaustive-below", type=int, default=200)
    parser.add_argument("--engine", choices=["new", "legacy"], default="new")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    if args.engine == "legacy":
        sys.path.insert(0, str(args.legacy_root.resolve()))
        for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
            del sys.modules[name]
        print(json.dumps(run_legacy(args)))
        return 0

    report = run_new(args)
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
