"""OpenAI organization usage and costs from the documented Admin APIs."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math

from app.core.usage_polling import usage_get

BASE = "https://api.openai.com/v1/organization"


def summarize(buckets, *, costs=False):
    total = Decimal(0)
    counts = {"input_tokens": 0, "output_tokens": 0, "num_model_requests": 0}
    for bucket in buckets:
        if not isinstance(bucket, dict) or not isinstance(bucket.get("results"), list):
            raise ValueError("Invalid usage bucket")
        for result in bucket["results"]:
            if not isinstance(result, dict):
                raise ValueError("Invalid usage result")
            if costs:
                amount = result.get("amount")
                if not isinstance(amount, dict) or amount.get("currency", "").lower() != "usd":
                    raise ValueError("Unsupported cost currency")
                value = amount.get("value")
                if isinstance(value, bool):
                    raise ValueError("Invalid cost")
                try:
                    value = Decimal(str(value))
                except InvalidOperation:
                    raise ValueError("Invalid cost") from None
                if not value.is_finite():
                    raise ValueError("Invalid cost")
                total += value
            else:
                for name in counts:
                    value = result.get(name, 0)
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise ValueError("Invalid usage count")
                    counts[name] += value
    if costs and not math.isfinite(float(total)):
        raise ValueError("Invalid cost total")
    return {"spend_month": float(total), "currency": "USD"} if costs else counts


async def fetch(admin_key, organization_id, *, costs=False):
    now = datetime.now(timezone.utc)
    start = int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    end = int(now.timestamp())
    url = BASE + ("/costs" if costs else "/usage/completions")
    response = await usage_get(
        url, headers={"Authorization": f"Bearer {admin_key}", "OpenAI-Organization": organization_id},
        params={"start_time": start, "end_time": end, "bucket_width": "1d", "limit": 31},
        paginate=True, interval=900,
    )
    response.raise_for_status()
    summary = summarize(response.json()["data"], costs=costs)
    return {**summary, "period_start": start, "period_end": end}
