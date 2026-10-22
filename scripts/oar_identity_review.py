"""Expert review of OAR similarity: old rule against new, on real targets and the real embedding model.

    # 1. inside the network, with sentence-transformers installed: draw targets, build the review files
    python -m scripts.oar_identity_review sample --targets 50 --out review/ [--include 000000004000000123:1300]

    # 2. give review/review_sheet.csv to an inventory or maintenance engineer (review/PROTOCOL.md)

    # 3. anywhere: measure the completed sheet
    python -m scripts.oar_identity_review evaluate --review review/review_sheet_completed.csv --key review/key.csv

WHY THIS EXISTS. ``oar-similarity-10`` makes identity a stage of its own and replaces the weighted mean by a
median. The unit tests prove the rules do what they say, on injected similarities. They cannot say whether
the rules find the RIGHT materials on VZI's real descriptions; only a person who knows the parts can. This is
the procedure in docs/i07_oar_similarity_engine.md ("Calibration"), built on ``oar.evaluation``, with the arms
this change needs.

WHAT THE REVIEW SHEET CONTAINS, per target: the union of
  * the OLD rule's top 5 (identity_validation off, weighted mean -- oar-similarity-9),
  * the NEW rule's top 5,
  * the best candidates the NEW identity stage REFUSED -- so a genuine match it lost is seen, not only the
    ones it kept,
  * a few random eligible donors, so the reviewer is not only shown what an engine liked.
The sheet is BLIND: descriptions, group, price and criticality, in shuffled order. Which arm a pair came from,
its scores and the engine's verdict are in ``key.csv``, which the reviewer never sees.

THIS SCRIPT DECIDES NOTHING. ``evaluate`` prints measurements and, against gates passed on the command line,
whether each is met. The gates' defaults are proposals for the business to confirm; no floor or vocabulary is
changed automatically (a changed floor is a reviewed ``SimilarityPolicy`` change with these numbers attached).

``sample`` needs the database AND the real MiniLM model, and stops if either is missing: a review run on a
stand-in would measure the stand-in. ``evaluate`` needs neither.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from app.initiatives.i7.oar import evaluation
from app.initiatives.i7.oar.types import CandidateAttributes

Key = tuple[str, str]

REVIEW_COLUMNS = (
    "pair_id", "target_material", "target_plant", "target_description", "target_group", "target_criticality",
    "target_price", "candidate_material", "candidate_plant", "candidate_description", "candidate_group",
    "candidate_price", "relevant", "reviewer", "note",
)
KEY_COLUMNS = (
    "pair_id", "target_material", "target_plant", "candidate_material", "candidate_plant", "arms",
    "old_rank", "new_rank", "text_similarity", "same_group", "combined", "identity_status", "identity_reason",
    "stratum",
)
ARMS = ("old", "new", "refused", "random")

PROTOCOL = """# OAR similarity review -- protocol

You are asked one question about each row of `review_sheet.csv`:

> Could this candidate part's stocking history (its safety stock, reorder point and maximum) reasonably stand
> in for the TARGET's, which has no usable history of its own?

Fill the `relevant` column with **yes** or **no**. Use **unsure** if you cannot tell from what is shown; those
rows are left out of the measurements and counted. Leave a row blank only if you have not reached it.

* Judge the PART, not whether the numbers look right. You are not told, and must not guess, what the system
  chose or refused: the rows are in shuffled order and may include parts the system rejected.
* "Yes" means a stand-in you would be comfortable defending to a planner: same kind of component, similar
  duty and cost. A different component from the same family (a spray nozzle for a filter) is **no**, however
  well the descriptions overlap.
* A different make or wording is fine if it is the same kind of part. Do not require identical descriptions.
* Use `note` for anything worth keeping, especially a surprising "no" or a part the descriptions hide.

Please fill `reviewer` with your name. Do not edit any other column or reorder rows.

Return the completed file. Rows judged: aim for all of them; about 15 candidates per target.
"""


# --- the pure parts: sampling, sheet building, measuring ----------------------------------------------


@dataclass(frozen=True)
class TargetInfo:
    key: Key
    criticality: str | None
    group: str | None
    has_description: bool
    has_component_type: bool
    has_part_number: bool
    eligible_donors: int

    @property
    def stratum(self) -> str:
        return "|".join(
            (
                self.criticality or "-",
                self.group or "-",
                "type" if self.has_component_type else "notype",
                "pn" if self.has_part_number else "nopn",
            )
        )


def choose_targets(
    pool: Sequence[TargetInfo],
    count: int,
    *,
    seed: int = 1,
    include: Sequence[Key] = (),
    focus_group: str | None = None,
    focus_count: int = 0,
    minimum_eligible: int = 5,
) -> list[TargetInfo]:
    """About ``count`` targets spread across criticality, material group and how much their description says.

    Only targets with a description and at least ``minimum_eligible`` eligible donors can be reviewed
    (without either there is nothing to compare). ``include`` are always taken -- the reported cases -- and
    ``focus_count`` targets are drawn from ``focus_group`` (the broad group the defect was found in). The rest
    is round-robin over strata, largest first, one target at a time, so no single big stratum fills the sample.
    Deterministic for a seed.
    """
    rng = random.Random(seed)
    usable = [t for t in pool if t.has_description and t.eligible_donors >= minimum_eligible]
    by_key = {t.key: t for t in usable}
    chosen: dict[Key, TargetInfo] = {}

    for key in include:
        if key in by_key:
            chosen[key] = by_key[key]

    if focus_group:
        focus = [t for t in usable if t.group == focus_group and t.key not in chosen]
        rng.shuffle(focus)
        # within the focus group, prefer a spread of description kinds
        focus.sort(key=lambda t: (t.has_component_type, t.has_part_number))
        for t in _spread(focus, focus_count):
            chosen[t.key] = t

    strata: dict[str, list[TargetInfo]] = defaultdict(list)
    for t in usable:
        if t.key not in chosen:
            strata[t.stratum].append(t)
    for members in strata.values():
        rng.shuffle(members)
    order = sorted(strata, key=lambda s: (-len(strata[s]), s))
    while len(chosen) < count and any(strata[s] for s in order):
        for stratum in order:
            if len(chosen) >= count:
                break
            if strata[stratum]:
                t = strata[stratum].pop()
                chosen[t.key] = t
    return list(chosen.values())


def _spread(items: Sequence[TargetInfo], count: int) -> list[TargetInfo]:
    """``count`` items taken evenly across an ordered list."""
    if count <= 0 or not items:
        return []
    if len(items) <= count:
        return list(items)
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


@dataclass
class PairRecord:
    target: Key
    candidate: Key
    arms: set[str]
    old_rank: int | None = None
    new_rank: int | None = None
    text_similarity: Decimal | None = None
    same_group: bool = False
    combined: Decimal | None = None
    identity_status: str | None = None
    identity_reason: str | None = None


def merge_pairs(
    target: Key,
    *,
    old_neighbours: Sequence[tuple[Key, Decimal | None, Decimal | None, bool]],
    new_neighbours: Sequence[tuple[Key, Decimal | None, Decimal | None, bool]],
    refused: Sequence[tuple[Key, Decimal | None, Decimal | None, bool, str | None, str | None]],
    random_donors: Sequence[tuple[Key, Decimal | None, Decimal | None, bool]],
    refused_limit: int = 5,
) -> list[PairRecord]:
    """One record per distinct candidate, with every arm it appeared in.

    Each tuple is ``(candidate, text_similarity, combined, same_group[, status, reason])``. Ranks are the
    1-based position in the old and new top lists.
    """
    pairs: dict[Key, PairRecord] = {}

    def record(candidate, text, combined, same_group) -> PairRecord:
        rec = pairs.setdefault(candidate, PairRecord(target, candidate, set()))
        rec.text_similarity = rec.text_similarity if rec.text_similarity is not None else text
        rec.combined = rec.combined if rec.combined is not None else combined
        rec.same_group = same_group
        return rec

    for rank, (candidate, text, combined, same_group) in enumerate(old_neighbours, start=1):
        rec = record(candidate, text, combined, same_group)
        rec.arms.add("old")
        rec.old_rank = rank
    for rank, (candidate, text, combined, same_group) in enumerate(new_neighbours, start=1):
        rec = record(candidate, text, combined, same_group)
        rec.arms.add("new")
        rec.new_rank = rank
    for candidate, text, combined, same_group, status, reason in list(refused)[:refused_limit]:
        rec = record(candidate, text, combined, same_group)
        rec.arms.add("refused")
        rec.identity_status, rec.identity_reason = status, reason
    for candidate, text, combined, same_group in random_donors:
        rec = record(candidate, text, combined, same_group)
        rec.arms.add("random")
    return list(pairs.values())


def blind_order(records: Sequence[PairRecord], seed: int) -> list[PairRecord]:
    """Shuffle so position carries no hint of an engine's ranking."""
    shuffled = list(records)
    random.Random(f"{seed}|{records[0].target if records else ''}").shuffle(shuffled)
    return shuffled


def _num(value) -> str:
    return "" if value is None else str(value)


def write_files(
    out: Path,
    targets: Mapping[Key, CandidateAttributes],
    candidates: Mapping[Key, CandidateAttributes],
    records_by_target: Mapping[Key, Sequence[PairRecord]],
    strata: Mapping[Key, str],
    *,
    seed: int,
) -> tuple[int, int]:
    """Write ``review_sheet.csv`` (blind), ``key.csv`` (engine facts) and ``PROTOCOL.md``."""
    out.mkdir(parents=True, exist_ok=True)
    pair_number = 0
    review_rows: list[dict] = []
    key_rows: list[dict] = []
    for target_key, records in records_by_target.items():
        target = targets[target_key]
        for rec in blind_order(list(records), seed):
            pair_number += 1
            pair_id = f"P{pair_number:04d}"
            cand = candidates[rec.candidate]
            review_rows.append({
                "pair_id": pair_id,
                "target_material": target_key[0], "target_plant": target_key[1],
                "target_description": target.description or "", "target_group": target.material_group or "",
                "target_criticality": target.criticality or "", "target_price": _num(target.unit_price),
                "candidate_material": rec.candidate[0], "candidate_plant": rec.candidate[1],
                "candidate_description": cand.description or "", "candidate_group": cand.material_group or "",
                "candidate_price": _num(cand.unit_price),
                "relevant": "", "reviewer": "", "note": "",
            })
            key_rows.append({
                "pair_id": pair_id,
                "target_material": target_key[0], "target_plant": target_key[1],
                "candidate_material": rec.candidate[0], "candidate_plant": rec.candidate[1],
                "arms": "+".join(a for a in ARMS if a in rec.arms),
                "old_rank": _num(rec.old_rank), "new_rank": _num(rec.new_rank),
                "text_similarity": _num(rec.text_similarity), "same_group": "1" if rec.same_group else "0",
                "combined": _num(rec.combined),
                "identity_status": rec.identity_status or "", "identity_reason": rec.identity_reason or "",
                "stratum": strata.get(target_key, ""),
            })
    for name, columns, rows in (
        ("review_sheet.csv", REVIEW_COLUMNS, review_rows),
        ("key.csv", KEY_COLUMNS, key_rows),
    ):
        with open(out / name, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    (out / "PROTOCOL.md").write_text(PROTOCOL, encoding="utf-8")
    return len(records_by_target), len(review_rows)


# --- measuring -----------------------------------------------------------------------------------------

_TRUE = evaluation._TRUE
_FALSE = evaluation._FALSE
UNSURE = {"unsure", "?", "u", "unclear", "cannot tell"}


@dataclass(frozen=True)
class Judged:
    """A reviewed pair joined to what the engines said about it."""

    pair_id: str
    target: Key
    candidate: Key
    relevant: bool
    arms: frozenset[str]
    old_rank: int | None
    new_rank: int | None
    text_similarity: Decimal | None
    same_group: bool
    combined: Decimal | None
    identity_status: str
    note: str | None


@dataclass(frozen=True)
class ReviewLoad:
    judged: list[Judged]
    unjudged: int
    unsure: int
    unknown_pair_ids: list[str]


def load_review(review_path: Path, key_path: Path) -> ReviewLoad:
    """Join the completed sheet to the key. A blank or "unsure" row is counted and left out -- an unreviewed
    pair is UNJUDGED, not irrelevant. A value that is none of yes/no/unsure is an error, never a skip."""
    with open(key_path, newline="", encoding="utf-8-sig") as handle:
        keys = {row["pair_id"]: row for row in csv.DictReader(handle)}
    judged: list[Judged] = []
    unjudged = unsure = 0
    unknown: list[str] = []
    with open(review_path, newline="", encoding="utf-8-sig") as handle:
        for line, row in enumerate(csv.DictReader(handle), start=2):
            verdict = (row.get("relevant") or "").strip().lower()
            if not verdict:
                unjudged += 1
                continue
            if verdict in UNSURE:
                unsure += 1
                continue
            if verdict in _TRUE:
                relevant = True
            elif verdict in _FALSE:
                relevant = False
            else:
                raise ValueError(f"line {line}: 'relevant' must be yes/no/unsure, got {verdict!r}")
            key = keys.get(row["pair_id"])
            if key is None:
                unknown.append(row["pair_id"])
                continue
            text = key["text_similarity"]
            combined = key["combined"]
            judged.append(
                Judged(
                    pair_id=row["pair_id"],
                    target=(key["target_material"], key["target_plant"]),
                    candidate=(key["candidate_material"], key["candidate_plant"]),
                    relevant=relevant,
                    arms=frozenset(a for a in key["arms"].split("+") if a),
                    old_rank=int(key["old_rank"]) if key["old_rank"] else None,
                    new_rank=int(key["new_rank"]) if key["new_rank"] else None,
                    text_similarity=Decimal(text) if text else None,
                    same_group=key["same_group"] == "1",
                    combined=Decimal(combined) if combined else None,
                    identity_status=key["identity_status"],
                    note=(row.get("note") or "").strip() or None,
                )
            )
    return ReviewLoad(judged, unjudged, unsure, unknown)


def rankings(judged: Iterable[Judged], arm: str) -> dict[Key, list[Key]]:
    """target -> candidates in the arm's order (``old`` or ``new``)."""
    attribute = f"{arm}_rank"
    out: dict[Key, list[tuple[int, Key]]] = defaultdict(list)
    for j in judged:
        rank = getattr(j, attribute)
        if rank is not None:
            out[j.target].append((rank, j.candidate))
    return {t: [c for _, c in sorted(pairs)] for t, pairs in out.items()}


def evaluate_review(
    judged: Sequence[Judged], *, k: int = 5, minimum_neighbours: int = 2, minimum_precision: float = 0.80
) -> dict:
    """Every measurement, as plain data. See ``render_report`` for what each means."""
    verdicts = evaluation.judgements(
        evaluation.LabelledPair(j.target, j.candidate, j.relevant) for j in judged
    )
    result: dict = {"pairs": len(judged), "targets": len(verdicts)}

    # 1. ranking quality: old rule against new, on the same reviewed pairs
    arms = {}
    for arm in ("old", "new"):
        ranked = rankings(judged, arm)
        # an arm's neighbours the reviewer has not judged count against it in the pessimistic figure
        arms[arm] = {
            "precision_at_k_judged": evaluation.precision_at_k(ranked, verdicts, k),
            "precision_at_k_pessimistic": evaluation.precision_at_k(ranked, verdicts, k, judged_only=False),
            "recall_at_k": evaluation.recall_at_k(ranked, verdicts, k),
            "genuine_neighbours": sum(
                1 for t, cs in ranked.items() for c in cs[:k] if verdicts.get(t, {}).get(c) is True
            ),
            "wrong_neighbours": sum(
                1 for t, cs in ranked.items() for c in cs[:k] if verdicts.get(t, {}).get(c) is False
            ),
            "targets_with_minimum_neighbours": sum(1 for cs in ranked.values() if len(cs) >= minimum_neighbours),
            "targets_with_minimum_genuine_neighbours": sum(
                1
                for t, cs in ranked.items()
                if sum(1 for c in cs[:k] if verdicts.get(t, {}).get(c) is True) >= minimum_neighbours
            ),
            "targets_ranked": len(ranked),
        }
    result["arms"] = arms

    # 2. the identity stage as a classifier of genuine equivalents (NEW rule), over every reviewed pair
    def passed(j: Judged) -> bool:
        return j.identity_status.startswith(("CONFIRMED", "NOT_ASSESSED"))

    known_status = [j for j in judged if j.identity_status]
    refused = [j for j in known_status if not passed(j)]
    kept = [j for j in known_status if passed(j)]
    genuine_total = sum(1 for j in known_status if j.relevant)
    lost = [j for j in refused if j.relevant]
    result["identity"] = {
        "reviewed_with_a_verdict": len(known_status),
        "genuine_total": genuine_total,
        "genuine_refused": len(lost),
        "false_rejection_rate": len(lost) / genuine_total if genuine_total else None,
        "kept_not_genuine": sum(1 for j in kept if not j.relevant),
        "refused_not_genuine": sum(1 for j in refused if not j.relevant),
        "genuine_refused_by_status": dict(Counter(j.identity_status for j in lost)),
        "genuine_refused_pairs": [
            {"pair_id": j.pair_id, "target": j.target, "candidate": j.candidate,
             "status": j.identity_status, "note": j.note}
            for j in lost
        ],
        "component_conflicts_judged_genuine": [
            {"pair_id": j.pair_id, "target": j.target, "candidate": j.candidate, "note": j.note}
            for j in lost if j.identity_status == "REJECTED_COMPONENT_CONFLICT"
        ],
    }

    # 3. text floors: what precision and recall would each threshold give, by route
    def sweep(selected: list[Judged]) -> dict:
        scored = [(j.text_similarity, j.relevant) for j in selected if j.text_similarity is not None]
        points = evaluation.threshold_sweep(scored, [Decimal("0.30") + Decimal("0.05") * i for i in range(14)])
        recommended = evaluation.recommend_threshold(points, minimum_precision, minimum_admitted=10)
        return {
            "pairs": len(scored),
            "relevant": sum(1 for _, ok in scored if ok),
            "points": [
                {"threshold": str(p.threshold), "admitted": p.admitted, "precision": p.precision, "recall": p.recall}
                for p in points
            ],
            "recommended": None if recommended is None else str(recommended.threshold),
        }

    # A pair the NEW rule refused for a component conflict is not a floor question: the floor never saw it.
    floor_pairs = [j for j in judged if j.identity_status != "REJECTED_COMPONENT_CONFLICT"]
    result["text_floor"] = {
        "same_group": sweep([j for j in floor_pairs if j.same_group]),
        "other_group": sweep([j for j in floor_pairs if not j.same_group]),
        "minimum_precision": minimum_precision,
        "current_floor": "0.50 (both routes)",
    }

    # 4. where the two rules disagree -- the pairs that decide whether the change helped
    only_old = [j for j in judged if "old" in j.arms and "new" not in j.arms]
    only_new = [j for j in judged if "new" in j.arms and "old" not in j.arms]
    result["disagreement"] = {
        "dropped_by_new": {"pairs": len(only_old), "genuine": sum(1 for j in only_old if j.relevant)},
        "added_by_new": {"pairs": len(only_new), "genuine": sum(1 for j in only_new if j.relevant)},
    }
    result["random_controls"] = {
        "pairs": sum(1 for j in judged if "random" in j.arms),
        "genuine": sum(1 for j in judged if "random" in j.arms and j.relevant),
    }
    return result


def gates(metrics: dict, *, maximum_false_rejection: float, minimum_precision: float, minimum_target_share: float) -> list[tuple[str, bool | None, str]]:
    """``(gate, met, detail)``. PROPOSED gates -- defaults are for the business to confirm, not a standard."""
    old, new = metrics["arms"]["old"], metrics["arms"]["new"]
    ident = metrics["identity"]
    out: list[tuple[str, bool | None, str]] = []

    p_new, p_old = new["precision_at_k_judged"], old["precision_at_k_judged"]
    out.append((
        f"new precision@k is at least {minimum_precision:.0%} and no worse than old",
        None if p_new is None or p_old is None else (p_new >= minimum_precision and p_new >= p_old),
        f"new {p_new if p_new is None else round(p_new, 3)} vs old {p_old if p_old is None else round(p_old, 3)}",
    ))
    rate = ident["false_rejection_rate"]
    out.append((
        f"identity stage loses at most {maximum_false_rejection:.0%} of genuine matches",
        None if rate is None else rate <= maximum_false_rejection,
        f"{ident['genuine_refused']} of {ident['genuine_total']} genuine candidates refused",
    ))
    targets = metrics["targets"]
    share = (new["targets_with_minimum_genuine_neighbours"] / targets) if targets else None
    out.append((
        f"at least {minimum_target_share:.0%} of targets keep the minimum number of GENUINE neighbours",
        None if share is None else share >= minimum_target_share,
        f"{new['targets_with_minimum_genuine_neighbours']} of {targets} targets (old: "
        f"{old['targets_with_minimum_genuine_neighbours']})",
    ))
    out.append((
        "no genuine match lost to a component-type conflict (the vocabulary is not over-reaching)",
        not ident["component_conflicts_judged_genuine"],
        f"{len(ident['component_conflicts_judged_genuine'])} such pair(s)",
    ))
    return out


def render_report(load: ReviewLoad, metrics: dict, gate_results: list, *, k: int) -> str:
    def f(value) -> str:
        return "n/a" if value is None else f"{value:.3f}" if isinstance(value, float) else str(value)

    lines = ["# OAR similarity review -- results", ""]
    lines += [
        f"Reviewed pairs used: **{metrics['pairs']}** across **{metrics['targets']}** targets "
        f"(unjudged {load.unjudged}, unsure {load.unsure}, unknown pair ids {len(load.unknown_pair_ids)}).",
        "",
        "These are measurements on a sample, not a guarantee; with ~50 targets a difference of a few "
        "percentage points is not distinguishable from chance. The reviewed pairs are what the engines chose, "
        "what the new rule refused, and random controls -- not an unbiased sample of all pairs, so the floor "
        "tables describe THIS population.",
        "",
        f"## Neighbours (top {k}): old rule against new",
        "",
        "| | old (oar-similarity-9) | new (oar-similarity-10) |", "|---|---|---|",
    ]
    old, new = metrics["arms"]["old"], metrics["arms"]["new"]
    for label, key in (
        ("precision@k (judged neighbours only)", "precision_at_k_judged"),
        ("precision@k (unjudged counted wrong)", "precision_at_k_pessimistic"),
        ("recall@k (of genuine matches found by anyone)", "recall_at_k"),
        ("genuine neighbours chosen", "genuine_neighbours"),
        ("wrong neighbours chosen", "wrong_neighbours"),
        ("targets with the minimum number of neighbours", "targets_with_minimum_neighbours"),
        ("targets with the minimum number of GENUINE neighbours", "targets_with_minimum_genuine_neighbours"),
    ):
        lines.append(f"| {label} | {f(old[key])} | {f(new[key])} |")

    d = metrics["disagreement"]
    lines += [
        "",
        "## Where the rules disagree",
        "",
        f"* Chosen by old, dropped by new: **{d['dropped_by_new']['pairs']}** pairs, "
        f"**{d['dropped_by_new']['genuine']}** judged genuine (the change helped if this is small).",
        f"* Chosen by new, not by old: **{d['added_by_new']['pairs']}** pairs, "
        f"**{d['added_by_new']['genuine']}** judged genuine.",
        f"* Random controls: {metrics['random_controls']['genuine']} of {metrics['random_controls']['pairs']} "
        "genuine (the base rate of a match among eligible donors).",
    ]

    ident = metrics["identity"]
    lines += [
        "",
        "## The identity stage",
        "",
        f"* Genuine candidates it refused: **{ident['genuine_refused']}** of {ident['genuine_total']} "
        f"(false-rejection rate {f(ident['false_rejection_rate'])}).",
        f"* Refused and not genuine (correct refusals): {ident['refused_not_genuine']}; "
        f"kept but not genuine (missed false matches): {ident['kept_not_genuine']}.",
        f"* Genuine matches refused, by reason: {ident['genuine_refused_by_status'] or 'none'}.",
    ]
    if ident["genuine_refused_pairs"]:
        lines += ["", "Genuine matches it lost (read these -- each one is a rule or vocabulary to fix):", ""]
        for p in ident["genuine_refused_pairs"]:
            lines.append(f"* {p['pair_id']} {p['target']} -> {p['candidate']}: {p['status']}"
                         + (f" -- \"{p['note']}\"" if p["note"] else ""))

    lines += ["", "## Text floors", "",
              f"Current floor {metrics['text_floor']['current_floor']}. Proposed rule of thumb: the lowest floor "
              f"whose precision is at least {metrics['text_floor']['minimum_precision']:.0%} on at least 10 pairs.", ""]
    for route in ("same_group", "other_group"):
        s = metrics["text_floor"][route]
        lines += [f"### {route.replace('_', ' ')} route ({s['pairs']} pairs, {s['relevant']} genuine)", "",
                  "| floor | admitted | precision | recall |", "|---|---|---|---|"]
        for p in s["points"]:
            lines.append(f"| {p['threshold']} | {p['admitted']} | {f(p['precision'])} | {f(p['recall'])} |")
        lines += ["", f"Recommended floor: **{s['recommended'] or 'none qualifies -- collect more reviews'}**", ""]

    lines += ["## Gates (PROPOSED -- confirm with the business before relying on them)", ""]
    for name, met, detail in gate_results:
        lines.append(f"* {'MET' if met else 'NOT MET' if met is False else 'NOT MEASURABLE'}: {name} -- {detail}")
    lines.append("")
    return "\n".join(lines)


# --- the orchestration that needs the database and the real model ---------------------------------------


def _key(a: CandidateAttributes) -> Key:
    return (a.sap_material_number, a.sap_plant_code)


def cmd_sample(args) -> int:  # pragma: no cover - needs the database and the real model
    from app.core.db import get_sessionmaker
    from app.initiatives.i7.oar import eligibility, identity, repository, service
    from app.initiatives.i7.oar.embedding_store import SqlEmbeddingStore
    from app.initiatives.i7.oar.text_similarity import MiniLmEmbeddingProvider, PrecomputedEmbeddingProvider
    from app.initiatives.i7.policy.dev_fixtures import default_policy

    provider = MiniLmEmbeddingProvider()
    if not provider.is_available:
        print(
            "The real embedding model (all-MiniLM-L6-v2, sentence-transformers) is not available here. "
            "A review built on a stand-in would measure the stand-in, so this stops. "
            "Install requirements-oar-text.txt and run again.",
            file=sys.stderr,
        )
        return 2

    policy_new = default_policy()
    similarity_old = policy_new.similarity.model_copy(
        update={"identity_validation": False, "estimator": "weighted_mean"}
    )
    policy_old = policy_new.model_copy(update={"similarity": similarity_old})
    config = identity.config_for(policy_new.similarity)
    history = policy_new.similarity.minimum_neighbour_history_months

    session_factory = get_sessionmaker()
    with session_factory() as session:
        targets = repository.load_targets(session)
        donors = repository.load_candidate_population(session)
        inventory = repository.load_inventory_values(session)
    donors, _ = service.dedupe_donors(donors)
    price_span = repository.price_range(donors)

    store = SqlEmbeddingStore(session_factory)
    embeddings, _stats = service.prepare_embeddings(
        provider, [t.description for t in targets] + [d.description for d in donors], store=store
    )
    if embeddings is None:
        print("The model produced no embeddings; stopping.", file=sys.stderr)
        return 2
    scoring_provider = PrecomputedEmbeddingProvider(embeddings, provider.model_name, provider.model_version)

    # eligible donor counts per target (hard filters only), then the sample
    from app.initiatives.i7.oar.retrieval import PopulationProfile

    profile = PopulationProfile(donors, history)
    infos = []
    for t in targets:
        eligible, _ = profile.profile(t)
        p = config.vocabulary.profile(t.description)
        infos.append(TargetInfo(_key(t), t.criticality, t.material_group, bool(t.description),
                                bool(p.types), bool(p.part_numbers), eligible))
    include = [tuple(item.split(":", 1)) for item in args.include]
    chosen = choose_targets(infos, args.targets, seed=args.seed, include=include,
                            focus_group=args.focus_group, focus_count=args.focus_count)
    print(f"{len(chosen)} targets chosen from {len(infos)} (strata: {len({c.stratum for c in chosen})})")

    by_target = {_key(t): t for t in targets}
    by_donor = {_key(d): d for d in donors}
    rng = random.Random(args.seed)
    records: dict[Key, list[PairRecord]] = {}
    strata = {c.key: c.stratum for c in chosen}
    review_targets: dict[Key, CandidateAttributes] = {}
    review_candidates: dict[Key, CandidateAttributes] = {}

    def row(entry_key, target, score):
        cand = by_donor[entry_key]
        same = bool(target.material_group and cand.material_group and target.material_group == cand.material_group)
        text = score.text.value if score.text.is_available else None
        return (entry_key, text, score.combined, same)

    for info in chosen:
        target = by_target[info.key]
        old = service.evaluate_target(target, donors, scoring_provider, price_span, inventory, policy_old)
        new = service.evaluate_target(target, donors, scoring_provider, price_span, inventory, policy_new)
        eligible, _ = eligibility.filter_candidates(target, donors, history)

        def scored(candidate):
            return service._score_pair(target, candidate, scoring_provider, price_span, policy_new.similarity)

        refused = []
        for c in eligible:
            s = scored(c)
            if s.identity is not None and not s.identity.passed and s.combined is not None:
                text = s.text.value if s.text.is_available else None
                same = bool(target.material_group and c.material_group and target.material_group == c.material_group)
                refused.append((s.combined, (_key(c), text, s.combined, same, s.identity.status.value, s.identity.reason)))
        refused.sort(key=lambda r: -r[0])

        taken = (
            {(n.material, n.plant) for n in old.neighbours}
            | {(n.material, n.plant) for n in new.neighbours}
            | {r[1][0] for r in refused[: args.refused]}
        )
        pool = [c for c in eligible if _key(c) not in taken]
        rng.shuffle(pool)
        random_rows = [row(_key(c), target, scored(c)) for c in pool[: args.random]]

        records[info.key] = merge_pairs(
            info.key,
            old_neighbours=[((n.material, n.plant), n.score.text.value, n.score.combined,
                             bool(n.same_material_group)) for n in old.neighbours],
            new_neighbours=[((n.material, n.plant), n.score.text.value, n.score.combined,
                             bool(n.same_material_group)) for n in new.neighbours],
            refused=[r[1] for r in refused],
            random_donors=random_rows,
            refused_limit=args.refused,
        )
        review_targets[info.key] = target
        for rec in records[info.key]:
            review_candidates[rec.candidate] = by_donor[rec.candidate]
            # The NEW rule's verdict on EVERY pair, including ones only the old rule chose: whether the new
            # stage would have refused a pair is the comparison that matters, and it is unknowable later.
            verdict = scored(by_donor[rec.candidate]).identity
            if verdict is not None:
                rec.identity_status, rec.identity_reason = verdict.status.value, verdict.reason

    n_targets, n_pairs = write_files(Path(args.out), review_targets, review_candidates, records, strata,
                                     seed=args.seed)
    (Path(args.out) / "sample.json").write_text(
        json.dumps({"seed": args.seed, "targets": [list(k) for k in records], "model": provider.model_version,
                    "policy_old": "identity_validation=False, estimator=weighted_mean",
                    "policy_new": "defaults"}, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {n_pairs} pairs for {n_targets} targets to {args.out}/ (give review_sheet.csv and PROTOCOL.md "
          "to the reviewer; keep key.csv away from them)")
    return 0


def cmd_evaluate(args) -> int:
    load = load_review(Path(args.review), Path(args.key))
    if not load.judged:
        print("No judged pairs in the sheet yet.", file=sys.stderr)
        return 1
    metrics = evaluate_review(load.judged, k=args.k, minimum_neighbours=args.minimum_neighbours,
                              minimum_precision=args.minimum_precision)
    results = gates(metrics, maximum_false_rejection=args.max_false_rejection,
                    minimum_precision=args.minimum_precision, minimum_target_share=args.min_target_share)
    report = render_report(load, metrics, results, k=args.k)
    print(report)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sample", help="draw targets and build the review files (database + real model)")
    s.add_argument("--targets", type=int, default=50)
    s.add_argument("--out", default="review")
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--include", action="append", default=[], metavar="MATERIAL:PLANT",
                   help="always review this target (repeatable); e.g. the reported filter")
    s.add_argument("--focus-group", default="308175", help="material group to over-sample")
    s.add_argument("--focus-count", type=int, default=8)
    s.add_argument("--refused", type=int, default=5, help="best identity-refused candidates per target")
    s.add_argument("--random", type=int, default=3, help="random eligible donors per target")
    s.set_defaults(func=cmd_sample)

    e = sub.add_parser("evaluate", help="measure a completed review sheet (no database, no model)")
    e.add_argument("--review", required=True)
    e.add_argument("--key", required=True)
    e.add_argument("--report")
    e.add_argument("-k", type=int, default=5)
    e.add_argument("--minimum-neighbours", type=int, default=2)
    e.add_argument("--minimum-precision", type=float, default=0.80)
    e.add_argument("--max-false-rejection", type=float, default=0.10)
    e.add_argument("--min-target-share", type=float, default=0.70)
    e.set_defaults(func=cmd_evaluate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
