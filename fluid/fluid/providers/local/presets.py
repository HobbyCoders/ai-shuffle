"""Opt-in local tuning candidates; deliberately NOT applied by any client/host.

These are model-vendor recommendations, not measured wins. The caller must
select the exact family and serving dialect, then pass the result as LocalClient
``extra`` only after benchmarking on that local server. Unknown families and
unsupported switches fail closed rather than guessing from a model name.

Qwen3 (original dense/MoE, NOT Qwen3-Coder, Qwen3.5 or Qwen3-Next):
https://huggingface.co/Qwen/Qwen3-8B#best-practices
llama.cpp's OpenAI-compatible endpoint accepts chat_template_kwargs; other
servers need their own explicitly tested template switch.
"""
from __future__ import annotations


def candidate_extra(family: str, *, reasoning: bool, dialect: str) -> dict:
    """Unactivated vendor sampling + template switch for an explicit family."""
    if family != "qwen3" or dialect != "llama.cpp":
        raise ValueError("No wire-tested preset for this family/server dialect")
    if not isinstance(reasoning, bool):
        raise ValueError("reasoning must be a boolean")
    return {
        "temperature": 0.6 if reasoning else 0.7,
        "top_p": 0.95 if reasoning else 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "chat_template_kwargs": {"enable_thinking": reasoning},
    }
