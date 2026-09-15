"""The model and prompt registry.

W1.5 asks for prompts and model choices to be *externalised*. Two reasons, and
the second is the one that matters here:

1. Changing a prompt should not be a code change.
2. **Provenance.** This programme is human-gated and audited. Every I07
   recommendation carries an LLM-written rationale that a person reads before
   approving a stock-level change. When someone asks six months later why a
   recommendation said what it said, "the model wrote it" is not an answer --
   the exact prompt, its version, and the model that ran it have to be
   recoverable. So a rendered prompt hands back its identity, and ``Completion``
   carries that identity through to the caller.

Prompts live as files under ``app/prompts/`` -- one directory per prompt, one
file per version::

    app/prompts/
      i07_recommendation_rationale/
        v1.md
        v2.md          <- highest version wins unless one is pinned
      i08_coding_candidate/
        v1.md

Markdown rather than a config format on purpose: these are paragraphs of English
that people review, and quoting them inside YAML or JSON makes them harder to
read and to diff.

Placeholders use ``{name}`` and are filled by keyword. A missing placeholder
raises rather than rendering a prompt with a literal ``{material}`` in it -- a
silently malformed prompt produces confident nonsense.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.ai import AIError

# backend/app/core/prompts.py -> backend/app/prompts
_PROMPT_ROOT = Path(__file__).resolve().parent.parent / "prompts"

_VERSION_FILE = re.compile(r"^v(\d+)\.md$")
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class PromptError(AIError):
    """A prompt is missing, unversioned, or rendered with the wrong variables."""


@dataclass(frozen=True)
class Prompt:
    """One version of one prompt, and its identity."""

    id: str
    version: int
    template: str

    @property
    def placeholders(self) -> frozenset[str]:
        return frozenset(_PLACEHOLDER.findall(self.template))

    def render(self, **values: object) -> str:
        """Fill the placeholders, refusing to guess.

        Missing values raise; extra values raise too. A prompt rendered with a
        typo'd variable name would otherwise reach the model with a literal
        ``{plant}`` in it and come back with plausible, wrong text.
        """
        required = self.placeholders
        provided = frozenset(values)

        missing = required - provided
        if missing:
            raise PromptError(
                f"{self.id} v{self.version} needs {sorted(missing)}, which were not "
                f"supplied. Required: {sorted(required)}."
            )

        unexpected = provided - required
        if unexpected:
            raise PromptError(
                f"{self.id} v{self.version} was given {sorted(unexpected)}, which it "
                f"does not use. Required: {sorted(required)}. A renamed placeholder "
                "usually means the prompt and the caller have drifted apart."
            )

        return self.template.format(**values)


def prompt_root() -> Path:
    return _PROMPT_ROOT


def set_prompt_root(path: Path) -> None:
    """Point the registry at a different directory.

    For tests. Writing prompt files into ``app/prompts/`` to exercise version
    selection would mutate the source tree, and a test that crashes mid-way
    would leave a stray directory behind for somebody to commit by accident.
    """
    global _PROMPT_ROOT
    _PROMPT_ROOT = path
    reset_prompt_cache()


@lru_cache
def _versions(prompt_id: str) -> dict[int, Path]:
    directory = _PROMPT_ROOT / prompt_id
    if not directory.is_dir():
        available = (
            ", ".join(sorted(p.name for p in _PROMPT_ROOT.iterdir() if p.is_dir()))
            if _PROMPT_ROOT.is_dir()
            else "none"
        )
        raise PromptError(f"No prompt directory for {prompt_id!r}. Available: {available}.")

    found: dict[int, Path] = {}
    for path in directory.iterdir():
        match = _VERSION_FILE.match(path.name)
        if match:
            found[int(match.group(1))] = path

    if not found:
        raise PromptError(
            f"{prompt_id} has no version files. Expected at least {directory / 'v1.md'}."
        )
    return found


def get_prompt(prompt_id: str, version: int | None = None) -> Prompt:
    """One prompt, at ``version`` or at the highest available.

    Pin a version wherever reproducibility matters more than improvement -- a
    regression test, or a rationale being compared against an earlier one.
    """
    versions = _versions(prompt_id)
    chosen = version if version is not None else max(versions)

    if chosen not in versions:
        raise PromptError(
            f"{prompt_id} has no v{chosen}. Available: {sorted(versions)}."
        )

    return Prompt(
        id=prompt_id,
        version=chosen,
        template=versions[chosen].read_text(encoding="utf-8").strip(),
    )


def available_prompts() -> list[str]:
    """Every prompt id in the registry."""
    if not _PROMPT_ROOT.is_dir():
        return []
    return sorted(p.name for p in _PROMPT_ROOT.iterdir() if p.is_dir() and any(p.iterdir()))


def reset_prompt_cache() -> None:
    """Forget the discovered versions. For tests that write prompt files."""
    _versions.cache_clear()
