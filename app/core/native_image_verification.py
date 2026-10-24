"""Reviewed API-key image verification records. Empty until an owner-paid probe.

Each provider has one enabled exact combination. Adding a record requires a
successful real-account generation + decoded artifact, observed billing, and
reviewed per-image USD price. Text-model discovery is NOT verification. This is
code-reviewed data, never an environment flag or a caller-supplied price.
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

    def settings(self):
        return {"model": self.model, "size": self.size, "quality": self.quality, "format": self.format}

    def price(self, prompt=None):
        amount = Decimal(self.usd_per_image)
        input_rate = Decimal(self.usd_per_million_input_tokens)
        if (not self.evidence.strip() or not amount.is_finite() or amount <= 0
                or not input_rate.is_finite() or input_rate < 0 or self.input_token_overhead < 0):
            raise ValueError("A verification needs evidence and valid observed USD rates.")
        # Catalog previews quote the maximum supported prompt; admission quotes
        # the actual prompt. No token estimate can turn an unknown rate into free.
        tokens = len(prompt.encode("utf-8")) if prompt is not None else 32000 * 4
        amount += input_rate * Decimal(tokens + self.input_token_overhead) / Decimal(1000000)
        return {"currency": "USD", "unit": "item", "amount": str(amount), "total": str(amount), "status": "estimated"}


# Official images/generations only; no custom gateway or subscription fallback.
VERIFIED_API_IMAGES: dict[str, Verification] = {}
ENDPOINTS = {"openai": "https://api.openai.com/v1/images/generations",
             "xai": "https://api.x.ai/v1/images/generations"}


def verified(kind):
    record = VERIFIED_API_IMAGES.get(kind) if kind in ENDPOINTS else None
    if record:
        record.price()  # An incomplete review must fail closed.
    return record
