"""Identity validation: is this candidate the same KIND of part as the target?

A stage of its own, between scoring and ranking. The combined score answers "how alike are these on the
attributes we hold?"; it cannot answer "are they the same component?", and ``scoring.combine`` used to be
asked to anyway -- a shared material group passed identity outright (oar-similarity-8/9).

THE DEFECT THIS CLOSES

Material group 308175 holds filters, spray nozzles, proximity sensors and wrenches. A target ``FILTER
PN:5112305242`` therefore accepted ``FILTER PRESS ANDRITZ SPRAY NOZZLE`` and ``FILTER PRESS ANDRITZ
PROXIMITY SENSOR`` as neighbours on the group alone, and their ROPs went into the estimate.

THE RULE, IN ORDER (first that applies decides)

1. **Component conflict** -- both descriptions name a component type from the vocabulary and no pair of them
   is compatible: ``REJECTED_COMPONENT_CONFLICT``. Only types a description names unmistakably count; a
   description naming none is *unknown*, and unknown is never a conflict.
2. **Shared part number** -- ``CONFIRMED_PART_NUMBER``. A part number is preserved verbatim (separators
   aside) rather than tokenised away.
3. **Same material group** needs description evidence as well. If both descriptions name a compatible
   component type, that type plus *either* text similarity at or above ``same_group_minimum_text_similarity``
   *or* a shared specific word is enough (``CONFIRMED_GROUP_COMPONENT_TYPE``). If the component type is
   unknown on either side, text similarity at or above that floor is enough (``CONFIRMED_GROUP_TEXT``) --
   unless both descriptions carry specific words, share none of them, and share only generic ones: then the
   similarity is resting on words like FILTER PRESS and ANDRITZ, and the answer is
   ``INSUFFICIENT_GENERIC_OVERLAP``. Anything else is ``INSUFFICIENT_GROUP_ONLY``. Two descriptions that
   share NO words at all are not treated as generic overlap: differently worded equivalents stay eligible.
4. **Different or unknown material group** -- text similarity at or above ``minimum_identity_text_similarity``
   (the 0.50 baseline of oar-similarity-8): ``CONFIRMED_TEXT``, else ``INSUFFICIENT_TEXT`` /
   ``INSUFFICIENT_NO_TEXT``. A genuine equivalent from another group is as eligible as it ever was.

Identical wording is never required: two descriptions are compared on the part numbers, component types
and specific words they carry, and on the text similarity the embedding model already produced. Identical
wording IS the strongest evidence there is, so when the model is unavailable two identical descriptions count
as text similarity 1.0 -- which is what the model would have said.

The same logic runs per pair here and vectorised over a whole partition in ``oar.retrieval`` (so the
shortlist never spends a slot on a candidate this stage will refuse). ``tests/i7/test_oar_identity.py`` holds
the two to identical verdicts.

THE VOCABULARY IS DATA, NOT CODE

``DEFAULT_COMPONENT_TYPES``, ``DEFAULT_CONTEXT_PHRASES`` and ``DEFAULT_GENERIC_TERMS`` are defaults for the
``SimilarityPolicy`` fields of the same names. They are conservative on purpose: every type is a way to
refuse a genuine equivalent, so only types a description names unmistakably are listed. Equipment names that
contain a part word ("FILTER PRESS") are *context phrases* and are set aside before types are matched, so
the machine's name does not read as the part.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.initiatives.i7.oar.types import IdentityStatus, IdentityVerdict
from app.initiatives.i7.policy.thresholds import ComponentTypeSpec, SimilarityPolicy

DEFAULT_COMPONENT_TYPES: tuple[ComponentTypeSpec, ...] = (
    ComponentTypeSpec(name="filter", patterns=("FILTER", "FILTER ELEMENT", "FILTER CARTRIDGE")),
    ComponentTypeSpec(name="nozzle", patterns=("NOZZLE", "SPRAY NOZZLE")),
    ComponentTypeSpec(name="sensor", patterns=("SENSOR",)),
    ComponentTypeSpec(
        name="proximity_sensor",
        patterns=("PROXIMITY SENSOR", "PROXIMITY SWITCH", "PROXIMITY", "INDUCTIVE SENSOR"),
        parent="sensor",
    ),
    ComponentTypeSpec(name="valve", patterns=("VALVE",)),
    ComponentTypeSpec(name="wrench", patterns=("WRENCH", "SPANNER")),
    ComponentTypeSpec(name="bearing", patterns=("BEARING",)),
    ComponentTypeSpec(name="bolt", patterns=("BOLT", "SCREW", "STUD")),
    ComponentTypeSpec(name="nut", patterns=("NUT",)),
    ComponentTypeSpec(name="seal", patterns=("SEAL", "GASKET", "O-RING", "ORING")),
    ComponentTypeSpec(name="hose", patterns=("HOSE",)),
    ComponentTypeSpec(name="belt", patterns=("BELT", "V-BELT")),
    ComponentTypeSpec(name="pump", patterns=("PUMP",)),
    ComponentTypeSpec(name="motor", patterns=("MOTOR",)),
    ComponentTypeSpec(name="gearbox", patterns=("GEARBOX", "GEAR BOX")),
    ComponentTypeSpec(name="cable", patterns=("CABLE",)),
    ComponentTypeSpec(name="relay", patterns=("RELAY",)),
    ComponentTypeSpec(name="fuse", patterns=("FUSE",)),
    ComponentTypeSpec(name="contactor", patterns=("CONTACTOR",)),
    ComponentTypeSpec(name="coupling", patterns=("COUPLING",)),
    ComponentTypeSpec(name="chain", patterns=("CHAIN",)),
    ComponentTypeSpec(name="spring", patterns=("SPRING",)),
)

DEFAULT_CONTEXT_PHRASES: tuple[str, ...] = ("FILTER PRESS", "PRESS FILTER")
"""Equipment, not part: the FILTER in "FILTER PRESS" says what machine the part belongs to."""

DEFAULT_GENERIC_TERMS: tuple[str, ...] = (
    # words that say little about what a part is
    "FILTER", "PRESS", "ASSY", "ASSEMBLY", "SPARE", "SPARES", "PART", "PARTS", "SET", "KIT", "STD",
    "STANDARD", "TYPE", "MODEL", "NEW", "GENUINE", "ORIGINAL", "ITEM", "FOR", "OF", "AND", "THE", "WITH",
    "TO", "IN", "ON", "NO", "NUMBER", "NOS", "PN", "P/N", "MAKE", "MAKER", "CODE",
    # manufacturers: a shared maker is not a shared function
    "ANDRITZ", "SKF", "FAG", "NSK", "SIEMENS", "ABB", "FLSMIDTH", "METSO", "SANDVIK", "GRUNDFOS",
)

MAX_TYPES = 62
"""Types are held as bits of a signed 64-bit integer so the shortlist can test a whole partition at once."""

MINIMUM_PART_NUMBER_LENGTH = 6
"""Characters, separators removed. Shorter digit-bearing tokens (``6310``, ``55KW``) are specifications."""

_TOKEN = re.compile(r"[A-Z0-9]+(?:[-/.][A-Z0-9]+)*")
_SEPARATORS = re.compile(r"[-/.]")
_UNIT = r"(?:MM|CM|M|KW|HP|V|A|KV|IN|BAR|MPA|KPA|PSI|RPM|HZ|KG|G|L|ML|MICRON|UM|DEG|NM|MTR|PCS)"
_SPECIFICATION = re.compile(
    rf"^(?:\d+(?:[.\-/]\d+)?{_UNIT}|\d+(?:\.\d+)?(?:X\d+(?:\.\d+)?)+(?:{_UNIT})?|M\d+(?:X\d+(?:\.\d+)?)?)$"
)


def tokenise(description: str | None) -> tuple[str, ...]:
    """Upper-case tokens; letters and digits, with ``-``, ``/`` and ``.`` kept inside a token so a part
    number such as ``6310-2RS`` survives whole. ``PN:5112305242`` yields ``PN`` and ``5112305242``."""
    if not description:
        return ()
    return tuple(_TOKEN.findall(description.upper()))


def _singular(token: str) -> str:
    """A plain-word token's singular, so BEARINGS and WRENCHES meet BEARING and WRENCH.

    Applied to the vocabulary and to descriptions alike, so only consistency matters, not grammar. A token
    carrying a digit is a part number or a specification and is left alone: ``6310-2RS`` must stay whole.
    """
    if len(token) <= 3 or any(ch.isdigit() for ch in token):
        return token
    if token.endswith("ES") and token[:-2].endswith(("CH", "SH", "X", "SS", "Z")):
        return token[:-2]
    if token.endswith("S") and not token.endswith(("SS", "US", "IS")):
        return token[:-1]
    return token


def compact(token: str) -> str:
    """A token without its separators -- how part numbers are compared."""
    return _SEPARATORS.sub("", token)


def is_part_number(token: str) -> bool:
    """A token that identifies one part: long enough, carries a digit, and is not a specification such as
    ``55KW``, ``100X50X6`` or ``M12X1.5``."""
    body = compact(token)
    if len(body) < MINIMUM_PART_NUMBER_LENGTH or not any(ch.isdigit() for ch in body):
        return False
    return _SPECIFICATION.match(token) is None


@dataclass(frozen=True)
class DescriptionProfile:
    """What one description says about the part, for the identity stage."""

    types: frozenset[str]
    """Component types named, from the vocabulary."""
    compatible_mask: int
    """Bits of every type compatible with any type named (itself, its ancestors, its descendants)."""
    type_mask: int
    """Bits of the types named."""
    part_numbers: frozenset[str]
    """Part numbers, separators removed."""
    distinctive: frozenset[str]
    """Specific words (including part numbers): everything that is not a generic term."""
    generic: frozenset[str] = frozenset()
    """The generic terms the description contains."""
    signature: tuple[str, ...] = ()
    """The description's tokens in order. Two equal signatures are the same description, however it is
    spaced or cased."""


EMPTY_PROFILE = DescriptionProfile(frozenset(), 0, 0, frozenset(), frozenset(), frozenset(), ())


class Vocabulary:
    """The component types, context phrases and generic terms, compiled for matching."""

    def __init__(
        self,
        types: tuple[ComponentTypeSpec, ...] = DEFAULT_COMPONENT_TYPES,
        context_phrases: tuple[str, ...] = DEFAULT_CONTEXT_PHRASES,
        generic_terms: tuple[str, ...] = DEFAULT_GENERIC_TERMS,
    ) -> None:
        names = [spec.name for spec in types]
        if len(set(names)) != len(names):
            raise ValueError("component type names must be unique")
        if len(names) > MAX_TYPES:
            raise ValueError(f"at most {MAX_TYPES} component types are supported")
        for spec in types:
            if spec.parent is not None and spec.parent not in names:
                raise ValueError(f"component type {spec.name!r} names unknown parent {spec.parent!r}")

        self.names = tuple(names)
        self.bit = {name: 1 << index for index, name in enumerate(names)}
        parent = {spec.name: spec.parent for spec in types}
        ancestors: dict[str, set[str]] = {}
        for name in names:
            chain: set[str] = set()
            current = parent[name]
            while current is not None:
                if current in chain or current == name:
                    raise ValueError(f"component type {name!r} has a cyclic parent chain")
                chain.add(current)
                current = parent[current]
            ancestors[name] = chain
        self.compatible_bits = {
            name: self.bit[name]
            | sum(self.bit[a] for a in ancestors[name])
            | sum(self.bit[other] for other in names if name in ancestors[other])
            for name in names
        }

        patterns: list[tuple[tuple[str, ...], str]] = []
        for spec in types:
            for pattern in spec.patterns:
                words = tuple(_singular(word) for word in tokenise(pattern))
                if words:
                    patterns.append((words, spec.name))
        patterns.sort(key=lambda item: (-len(item[0]), item[1]))
        self._patterns = tuple(patterns)

        contexts = [tuple(_singular(word) for word in tokenise(phrase)) for phrase in context_phrases]
        self._contexts = tuple(sorted((c for c in contexts if c), key=len, reverse=True))
        self.generic = frozenset(_singular(word) for term in generic_terms for word in tokenise(term))
        self._cache: dict[str, DescriptionProfile] = {}

    def _matches(self, words: list[str], start: int, pattern: tuple[str, ...]) -> bool:
        if start + len(pattern) > len(words):
            return False
        return all(words[start + i] == pattern[i] for i in range(len(pattern)))

    def profile(self, description: str | None) -> DescriptionProfile:
        """The profile of one description, cached: the same strings recur across millions of pairs."""
        if not description:
            return EMPTY_PROFILE
        cached = self._cache.get(description)
        if cached is not None:
            return cached
        profile = self._profile(description)
        if len(self._cache) >= 500_000:
            self._cache.clear()
        self._cache[description] = profile
        return profile

    def _profile(self, description: str) -> DescriptionProfile:
        tokens = tokenise(description)
        if not tokens:
            return EMPTY_PROFILE
        words = [_singular(token) for token in tokens]
        consumed = [False] * len(words)

        # Left to right, longest phrase first, never overlapping: in FILTER PRESS FILTER ELEMENT the phrase
        # PRESS FILTER must not reach back across FILTER PRESS and swallow the part's own FILTER.
        for i in range(len(words)):
            if consumed[i]:
                continue
            for context in self._contexts:
                if self._matches(words, i, context) and not any(consumed[i : i + len(context)]):
                    for j in range(i, i + len(context)):
                        consumed[j] = True
                    break

        found: set[str] = set()
        names_a_type = [False] * len(words)
        for i in range(len(words)):
            if consumed[i]:
                continue
            for pattern, name in self._patterns:
                if self._matches(words, i, pattern) and not any(consumed[i : i + len(pattern)]):
                    found.add(name)
                    for j in range(i, i + len(pattern)):
                        consumed[j] = True
                        names_a_type[j] = True
                    break

        part_numbers = frozenset(compact(token) for token in tokens if is_part_number(token))
        # The words that name the component type are the type, already counted as evidence of its own; a
        # shared BEARING must not also count as "something specific in common".
        distinctive = frozenset(
            compact(token) if is_part_number(token) else word
            for token, word, is_type_word in zip(tokens, words, names_a_type)
            if not is_type_word
            and word not in self.generic
            and token not in self.generic
            and len(word) > 1
        )
        generic = frozenset(
            word for token, word in zip(tokens, words) if word in self.generic or token in self.generic
        )
        return DescriptionProfile(
            types=frozenset(found),
            compatible_mask=sum(self.compatible_bits[name] for name in found),
            type_mask=sum(self.bit[name] for name in found),
            part_numbers=part_numbers,
            distinctive=distinctive,
            generic=generic,
            signature=tokens,
        )


@dataclass(frozen=True)
class IdentityConfig:
    """The identity stage's settings, resolved from a ``SimilarityPolicy``."""

    vocabulary: Vocabulary
    text_floor: Decimal | None
    """Text similarity a candidate outside the target's material group needs (the 0.50 baseline)."""
    same_group_floor: Decimal | None
    """Text similarity a same-group candidate needs without a shared part number or component type."""
    conflict_enabled: bool

    @classmethod
    def from_policy(cls, policy: SimilarityPolicy) -> "IdentityConfig":
        def decimal(value: float | None) -> Decimal | None:
            return None if value is None else Decimal(str(value))

        return cls(
            vocabulary=Vocabulary(
                policy.identity_component_types or DEFAULT_COMPONENT_TYPES,
                policy.identity_context_phrases
                if policy.identity_context_phrases is not None
                else DEFAULT_CONTEXT_PHRASES,
                policy.identity_generic_terms
                if policy.identity_generic_terms is not None
                else DEFAULT_GENERIC_TERMS,
            ),
            text_floor=decimal(policy.minimum_identity_text_similarity),
            same_group_floor=decimal(policy.same_group_minimum_text_similarity),
            conflict_enabled=policy.identity_component_conflict,
        )


_CONFIGS: dict[int, tuple[SimilarityPolicy, IdentityConfig]] = {}


def config_for(policy: SimilarityPolicy) -> IdentityConfig:
    """The resolved config for a policy, built once per policy object.

    Keyed by identity and holding the policy so its id cannot be reused: scoring calls this once per pair,
    and hashing a policy or recompiling a vocabulary each time would dominate the run.
    """
    cached = _CONFIGS.get(id(policy))
    if cached is not None and cached[0] is policy:
        return cached[1]
    config = IdentityConfig.from_policy(policy)
    if len(_CONFIGS) >= 64:
        _CONFIGS.clear()
    _CONFIGS[id(policy)] = (policy, config)
    return config


def compatible(target: DescriptionProfile, candidate: DescriptionProfile) -> bool:
    """Whether any type the target names is compatible with any type the candidate names."""
    return bool(target.compatible_mask & candidate.type_mask)


def assess(
    target: DescriptionProfile,
    candidate: DescriptionProfile,
    *,
    same_material_group: bool,
    text_similarity: Decimal | None,
    config: IdentityConfig,
) -> IdentityVerdict:
    """The identity verdict for one target-candidate pair. See the module docstring for the rule."""
    if text_similarity is None and target.signature and target.signature == candidate.signature:
        text_similarity = Decimal(1)
    types = (tuple(sorted(target.types)), tuple(sorted(candidate.types)))
    shared_numbers = tuple(sorted(target.part_numbers & candidate.part_numbers))

    def verdict(status: IdentityStatus, reason: str) -> IdentityVerdict:
        return IdentityVerdict(status, reason, types[0], types[1], shared_numbers, text_similarity)

    type_match = bool(target.types and candidate.types and compatible(target, candidate))
    if config.conflict_enabled and target.types and candidate.types and not type_match:
        return verdict(
            IdentityStatus.REJECTED_COMPONENT_CONFLICT,
            "component type conflict: target is "
            f"{'/'.join(types[0])}, candidate is {'/'.join(types[1])}",
        )

    if shared_numbers:
        return verdict(
            IdentityStatus.CONFIRMED_PART_NUMBER, f"shared part number {', '.join(shared_numbers)}"
        )

    text = f"{text_similarity:.2f}" if text_similarity is not None else None

    if same_material_group:
        floor = config.same_group_floor
        if floor is None:
            return verdict(
                IdentityStatus.CONFIRMED_GROUP_UNVERIFIED,
                "same material group (description evidence not required)",
            )
        text_ok = text_similarity is not None and text_similarity >= floor
        shared_specific = bool(target.distinctive & candidate.distinctive)
        if type_match:
            if text_ok or shared_specific:
                return verdict(
                    IdentityStatus.CONFIRMED_GROUP_COMPONENT_TYPE,
                    f"same material group and component type {'/'.join(types[0])}"
                    + (f", text similarity {text}" if text_ok else ", shared specific wording"),
                )
        elif text_ok:
            shared_generic = target.generic & candidate.generic
            generic_driven = (
                bool(target.distinctive)
                and bool(candidate.distinctive)
                and not shared_specific
                and bool(shared_generic)
            )
            if generic_driven:
                return verdict(
                    IdentityStatus.INSUFFICIENT_GENERIC_OVERLAP,
                    f"no identity match: text similarity {text} rests on generic words "
                    f"({', '.join(sorted(shared_generic))}) -- both descriptions carry "
                    "specific words and share none",
                )
            return verdict(
                IdentityStatus.CONFIRMED_GROUP_TEXT, f"same material group and text similarity {text}"
            )
        return verdict(
            IdentityStatus.INSUFFICIENT_GROUP_ONLY,
            "no identity match: same material group is not identity on its own, and "
            + ("no text comparison" if text is None else f"text similarity {text} below {floor}")
            + (" (component type agrees, but nothing specific is shared)" if type_match else ""),
        )

    floor = config.text_floor
    if floor is None:
        return verdict(IdentityStatus.NOT_ASSESSED, "identity text requirement switched off")
    if text_similarity is None:
        return verdict(
            IdentityStatus.INSUFFICIENT_NO_TEXT,
            "no identity match: different material group and no text comparison",
        )
    if text_similarity < floor:
        return verdict(
            IdentityStatus.INSUFFICIENT_TEXT,
            f"no identity match: different material group and text similarity {text} below {floor}",
        )
    return verdict(IdentityStatus.CONFIRMED_TEXT, f"text similarity {text} at or above {floor}")


def assess_descriptions(
    target_description: str | None,
    candidate_description: str | None,
    *,
    same_material_group: bool,
    text_similarity: Decimal | None,
    config: IdentityConfig,
) -> IdentityVerdict:
    """:func:`assess` from the raw descriptions."""
    vocabulary = config.vocabulary
    return assess(
        vocabulary.profile(target_description),
        vocabulary.profile(candidate_description),
        same_material_group=same_material_group,
        text_similarity=text_similarity,
        config=config,
    )
