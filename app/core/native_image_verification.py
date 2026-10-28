"""Reviewed API-key image verification records from owner-paid live probes.

Each provider has one enabled exact combination. Adding a record requires a
successful real-account generation + decoded artifact, observed billing, and
reviewed per-image USD price. Text-model discovery is NOT verification. This is
code-reviewed data, never an environment flag or a caller-supplied price.
Every other model, size, quality or format stays disabled until it has its own probe.
"""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Verification:
    model: str
    size: str
    quality: str | None
    format: str
    usd_per_image: str
    evidence: str
    # Text-input rate as well as output cost: OpenAI image calls can bill both.
    # UTF-8 bytes bound token count conservatively, plus request overhead.
    usd_per_million_input_tokens: str
    input_token_overhead: int = 1024

    def settings(self) -> dict[str, str | None]:
        return {
            "model": self.model, "size": self.size,
            "quality": self.quality, "format": self.format,
        }

    def price(self, prompt: str | None = None) -> dict[str, str]:
        amount = Decimal(self.usd_per_image)
        input_rate = Decimal(self.usd_per_million_input_tokens)
        if (not self.evidence.strip() or not amount.is_finite() or amount <= 0
                or not input_rate.is_finite() or input_rate < 0 or self.input_token_overhead < 0):
            raise ValueError("A verification needs evidence and valid observed USD rates.")
        # Catalog previews quote the maximum supported prompt; admission quotes
        # the actual prompt. No token estimate can turn an unknown rate into free.
        tokens = len(prompt.encode("utf-8")) if prompt is not None else 32000 * 4
        amount += input_rate * Decimal(tokens + self.input_token_overhead) / Decimal(1000000)
        return {
            "currency": "USD", "unit": "item", "amount": str(amount),
            "total": str(amount), "status": "estimated",
        }


# Official images/generations only; no custom gateway or subscription fallback.
# Evidence is in issue #541 (named runs, sanitized). Output bounds are reviewed
# ceilings above the observed charge, never the observed charge alone.
VERIFIED_API_IMAGES: dict[str, Verification] = {
    # One 1024x1024 PNG: 14 text-input + 196 image-output tokens (~$0.006 at
    # $5/M input, $30/M image output). Bound: $0.02 output (~667 tokens).
    "openai": Verification(
        "gpt-image-2.5-sunburst-2026-09-08", "1024x1024", "low", "png", "0.02",
        "generation-379.openai-image.r1028 (#541, 2026-10-28)", "5",
    ),
    # One 1024x1024 JPEG: provider-reported cost $0.02 per image with the
    # prompt (cost_in_usd_ticks 200000000), so no separate text-input rate.
    "xai": Verification(
        "grok-imagine-image", "1k", None, "jpeg", "0.02",
        "generation-379.xai-image.r1028 (#541, 2026-10-28)", "0",
    ),
}
ENDPOINTS = {"openai": "https://api.openai.com/v1/images/generations",
             "xai": "https://api.x.ai/v1/images/generations"}


def verified(kind: str) -> Verification | None:
    record = VERIFIED_API_IMAGES.get(kind) if kind in ENDPOINTS else None
    if record:
        record.price()  # An incomplete review must fail closed.
    return record
