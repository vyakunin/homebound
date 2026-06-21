"""Prompt-resilience layer — keep the fine-tuned model STEERABLE by its system
prompt instead of baking one fixed string into the weights.

If every training example carries the byte-identical system string, the model
learns to ignore the system turn and bakes the behaviour into weights → serve-time
prompt edits stop doing anything. Fix: sample one of several **equivalent** system
framings per example. Wording/order vary while MEANING is constant ⇒ the model
learns "attend to the meaning of the system turn", which is what makes it editable
at serve time.

Scope (deliberate): this samples the SHORT persona / reply framings (the ~65% DB
objectives), which are safe to paraphrase by hand without drifting. The VOICE is
invariant (the LoRA/FT's constant job — never varied here). The long served
persona file (grounded/abstention/transfer system) is NOT mechanically
paraphrased — auto-rewording an 8KB bilingual persona risks semantic drift, and
faithfulness is the #1 dataset killer; authored persona-file variants can be
passed in later via ``variants=``.

NOT yet here (documented as the next step in docs/SFT_PLAN.md): the *contrastive*
behaviour-coupled knobs (language-default / length-register / scope-refuse /
counterfactual-fact), because each needs a PAIRED target generated in the
specified language/length/stance — fabricating those poorly would poison the set.
"""
from __future__ import annotations

from random import Random

# De-named (decision 4) equivalent framings for the persona objective. Same
# meaning ("you are the author, write a post in your own voice"), varied wording
# and order. The first entry matches the historical canonical string so a build
# without sampling reproduces it.
PERSONA_VARIANTS: tuple[str, ...] = (
    "You are the author. Write in your own voice and style.",
    "Write the next post as the author, in your own voice and style.",
    "You are the author. Compose a post in your own voice.",
    "Speak as the author. Write a post in your natural voice and style.",
    "You are the author — write a post the way you actually write.",
)

# De-named equivalent framings for the reply objective (parent post → his reply).
REPLY_VARIANTS: tuple[str, ...] = (
    "You are the author. Respond in your own voice and style to the post below.",
    "Reply as the author, in your own voice, to the post below.",
    "You are the author. Answer the post below in your natural voice and style.",
    "As the author, respond to the post below in your own voice and style.",
    "You are the author — reply to the post below the way you actually would.",
)

# Equivalent USER-turn instructions for the persona objective. The persona set is
# the single largest objective (~12k), and v3 used the byte-identical user turn
# "Write a post." on every one of them → the model bakes an unconditional
# "respond to anything tersely" prior keyed on that exact string (the 7B run's
# main weakness). Varying the instruction (meaning constant) breaks that prior
# without dropping or generating any data. The first entry is the historical
# canonical string so a build without sampling reproduces it byte-for-byte.
PERSONA_USER_VARIANTS: tuple[str, ...] = (
    "Write a post.",
    "Write a post in your own voice.",
    "Post something.",
    "Share a new post.",
    "Write your next post.",
    "Write a short post about whatever's on your mind.",
)


def sample_user(variants: tuple[str, ...], key: str, seed: int) -> str:
    """Deterministically pick one user-instruction variant for an example.

    Same mechanism as :func:`sample_system` but on a distinct sub-stream
    (``usr``) so the system and user choices for one example are independent."""
    if not variants:
        raise ValueError("variants must be non-empty")
    if len(variants) == 1:
        return variants[0]
    return Random(f"{seed}:usr:{key}").choice(variants)


def sample_system(variants: tuple[str, ...], key: str, seed: int) -> str:
    """Deterministically pick one system-prompt variant for an example.

    Keyed on the example's own content (``key``) so the choice is stable across
    rebuilds and independent of ordering/concurrency, while still spreading
    variants across the dataset. Empty/singleton variant lists return the sole
    entry."""
    if not variants:
        raise ValueError("variants must be non-empty")
    if len(variants) == 1:
        return variants[0]
    return Random(f"{seed}:sys:{key}").choice(variants)
