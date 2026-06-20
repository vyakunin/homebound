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
