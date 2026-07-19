"""R3 live validation — ONE upstream /responses request carrying BOTH resent
reasoning items AND function_call + function_call_output is accepted & answered.

Closure record: docs/CODEX_SPIKE_FINDINGS.md "R3 CLOSED" (2026-07-19, PASS).

Exercises the REAL gateway code path via httpx ASGITransport (no uvicorn) against
the real ChatGPT/Codex backend. stream:false on every request (finite body ->
ASGITransport-safe; stream:true through ASGITransport deadlocks). Secret hygiene:
never prints tokens/keys/headers/signatures (signature -> length only);
assistant text excerpts capped at 200 chars.

Live-quota-consuming — NOT part of the automated suite. Run manually:

    DATABASE_URL=postgresql://...   # DB holding the Codex provider row
    ADMIN_PASSWORD=...              # unlocks the at-rest encryption key
    R3_PROVIDER_ID=<uuid>           # Codex provider row to route through
    python tools/r3_live_validation.py

Behavioral gotcha (proven live): trivially-easy prompts yield ZERO reasoning
items even at effort=high on gpt-5.6-terra/sol — the phase-1 prompt below is
deliberately reasoning-demanding. Do not "simplify" it.
"""

from __future__ import annotations

import os
import sys

# DATABASE_URL must point at the target DB BEFORE any app module is imported.
if not os.environ.get("DATABASE_URL"):
    sys.exit("Set DATABASE_URL (the DB holding the Codex provider row) first.")

PROVIDER_ID = os.environ.get("R3_PROVIDER_ID", "")
if not PROVIDER_ID:
    sys.exit("Set R3_PROVIDER_ID to the Codex model_providers row UUID.")

import asyncio
import json
from typing import Any, Dict, List, Tuple

import httpx

from app.core.config import settings
from app.db.base import init_pool
from app.db import database as db
from app.core import encryption
from app.core.translator_gateway.service import build_gateway_app
from app.core.translator_gateway.routing import mint_session_token
from app.core.translator_gateway.anthropic_to_responses import translate_request

# ── Live request budget ───────────────────────────────────────────────────────
_MAX_LIVE = 8
_live_count = 0


def _excerpt(s: str, n: int = 200) -> str:
    s = (s or "").replace("\n", " ").replace("\r", " ").strip()
    return s[:n]


TOOLS = [
    {
        "name": "get_weather",
        "description": "Get the current weather for a location.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City name, e.g. 'Paris, France'",
                }
            },
            "required": ["location"],
        },
    }
]

# High effort (budget >= 16384 -> 'high'); max_tokens generous.
THINKING = {"type": "enabled", "budget_tokens": 16384}
MAX_TOKENS = 20000


def _phase1_body(model: str) -> Dict[str, Any]:
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "thinking": THINKING,
        "tools": TOOLS,
        "tool_choice": {"type": "auto"},
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Think carefully step by step. I have three friends: Ann is in "
                    "Tokyo, Bob is in Cairo, and Cara is in Lima. I want to visit "
                    "whichever of them is currently experiencing the coldest weather, "
                    "but I only have the budget to check ONE city's weather. Reason "
                    "about which single city is most likely to be coldest RIGHT NOW "
                    "given the current season in each hemisphere, explain your "
                    "reasoning, then call get_weather exactly once for that city."
                ),
            }
        ],
    }


def _count_blocks(content: List[Dict[str, Any]]) -> Tuple[int, int, int]:
    thinking = sum(1 for b in content if b.get("type") == "thinking")
    tool_use = sum(1 for b in content if b.get("type") == "tool_use")
    text = sum(1 for b in content if b.get("type") == "text")
    return thinking, tool_use, text


async def _post(client: httpx.AsyncClient, headers: Dict[str, str], body: Dict[str, Any]):
    global _live_count
    if _live_count >= _MAX_LIVE:
        raise RuntimeError(f"LIVE BUDGET EXHAUSTED ({_MAX_LIVE}); stopping.")
    _live_count += 1
    print(f"  -> live POST /v1/messages #{_live_count} (model={body.get('model')})")
    resp = await client.post("/v1/messages", headers=headers, json=body)
    return resp


async def main() -> int:
    # Sanity: confirm settings resolved the intended DB (host only; no password).
    dsn = settings.database_url or ""
    dbhost = dsn.split("@")[-1] if "@" in dsn else "?"
    print(f"DB target (host/db only): {dbhost}")
    init_pool(min_conn=1, max_conn=4)

    # Load the at-rest encryption key exactly as the app does at startup
    # (ADMIN_PASSWORD env / settings.admin_password + existing salt in the DB).
    # get_or_create_salt reads the salt that already exists (Codex tokens are
    # already encrypted with it) — no salt is generated / written.
    if not encryption.init_encryption_from_env(db):
        print(
            "PRECONDITION FAILED: encryption key could not be loaded "
            "(ADMIN_PASSWORD / settings.admin_password not available). "
            "Cannot decrypt Codex credentials. STOPPING."
        )
        return 2
    print(f"encryption ready: {encryption.is_encryption_ready()}")

    app = build_gateway_app()
    token = mint_session_token(None, PROVIDER_ID)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    report: Dict[str, Any] = {}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway", timeout=600.0
    ) as client:
        # ── PHASE 1: get a real turn with thinking + tool_use ────────────────
        print("PHASE 1: eliciting thinking + tool_use ...")
        ladder = [("gpt-5.6-terra", "primary"), ("gpt-5.6-sol", "fallback")]
        phase1_json = None
        phase1_model = None
        p1_status = None
        for model, label in ladder:
            resp = await _post(client, headers, _phase1_body(model))
            p1_status = resp.status_code
            if resp.status_code != 200:
                print(f"  [{label}] HTTP {resp.status_code}: {_excerpt(resp.text, 300)}")
                # try next model on non-200
                continue
            data = resp.json()
            content = data.get("content", [])
            n_think, n_tool, n_text = _count_blocks(content)
            print(
                f"  [{label} {model}] HTTP 200 stop={data.get('stop_reason')} "
                f"thinking={n_think} tool_use={n_tool} text={n_text}"
            )
            if n_tool >= 1 and n_think >= 1:
                phase1_json = data
                phase1_model = model
                break
            if n_tool >= 1 and n_think == 0:
                print("  tool_use present but ZERO thinking -> escalating model")
                continue
            if n_tool == 0:
                print("  NO tool_use -> escalating model")
                continue

        if phase1_json is None or phase1_model is None:
            report["verdict"] = "FAIL — could not obtain a turn with BOTH thinking and tool_use"
            report["live_requests"] = _live_count
            report["phase1_last_status"] = p1_status
            print("\n=== R3 RESULT ===")
            print(json.dumps(report, indent=2))
            return 1

        p1_content = phase1_json["content"]
        n_think1, n_tool1, _ = _count_blocks(p1_content)
        p1_usage = phase1_json.get("usage", {})

        # Locate the thinking block (with signature) and the tool_use block.
        thinking_block = next(b for b in p1_content if b.get("type") == "thinking")
        tool_use_block = next(b for b in p1_content if b.get("type") == "tool_use")
        sig = thinking_block.get("signature") or ""
        print(
            f"  captured thinking(sig len={len(sig)}) + "
            f"tool_use(name={tool_use_block.get('name')})"
        )

        # ── Build PHASE 2 history: user, assistant(blocks VERBATIM), user(tool_result)
        assistant_content = list(p1_content)  # verbatim, signature intact
        # Distinctive, unguessable values so a coherent final answer MUST reflect
        # the tool result if it used it.
        WX_TEMP = 41
        WX_COND = "freezing drizzle"
        WX_WIND = 19
        tool_result_value = json.dumps(
            {
                "location": (tool_use_block.get("input") or {}).get("location", "the chosen city"),
                "temperature_f": WX_TEMP,
                "conditions": WX_COND,
                "wind_mph": WX_WIND,
            }
        )
        phase2_body = {
            "model": phase1_model,
            "max_tokens": MAX_TOKENS,
            "thinking": THINKING,
            "tools": TOOLS,
            "tool_choice": {"type": "auto"},
            "stream": False,
            "messages": [
                _phase1_body(phase1_model)["messages"][0],
                {"role": "assistant", "content": assistant_content},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_block.get("id"),
                            "content": tool_result_value,
                        }
                    ],
                },
            ],
        }

        # ── EVIDENCE: translate phase-2 body exactly as the router does. ──────
        translated = translate_request(
            phase2_body, upstream_mode="codex", target_model=phase1_model
        )
        item_types = [it.get("type") for it in translated.get("input", [])]
        report["phase2_item_type_sequence"] = item_types
        has_reasoning = "reasoning" in item_types
        has_fc = "function_call" in item_types
        has_fco = "function_call_output" in item_types
        print(f"\nPHASE 2 translated input item types: {item_types}")
        print(
            f"  reasoning={has_reasoning} function_call={has_fc} "
            f"function_call_output={has_fco}"
        )

        # ── PHASE 2 live call ────────────────────────────────────────────────
        print("\nPHASE 2: resending full history (reasoning + fc + fco) ...")
        resp2 = await _post(client, headers, phase2_body)
        p2_status = resp2.status_code
        report["phase2_http_status"] = p2_status
        if p2_status != 200:
            report["verdict"] = f"FAIL — phase 2 HTTP {p2_status}"
            report["phase2_error"] = _excerpt(resp2.text, 300)
            report["live_requests"] = _live_count
            print("\n=== R3 RESULT ===")
            print(json.dumps(report, indent=2))
            return 1

        data2 = resp2.json()
        p2_content = data2.get("content", [])
        p2_text = "".join(
            b.get("text", "") for b in p2_content if b.get("type") == "text"
        )
        p2_usage = data2.get("usage", {})
        _, _, n_text2 = _count_blocks(p2_content)

        # Did the final answer actually reflect the injected tool result?
        low = p2_text.lower()
        markers = [str(WX_TEMP), "drizzle", "freez", str(WX_WIND)]
        matched = [m for m in markers if m.lower() in low]
        reflects_tool_result = len(matched) > 0
        # Build an excerpt centered on the first marker hit (<=200 chars).
        excerpt_reflect = _excerpt(p2_text, 200)
        if matched:
            i = low.find(matched[0].lower())
            start = max(0, i - 60)
            excerpt_reflect = _excerpt(p2_text[start : start + 200], 200)

        # ── Verdict ──────────────────────────────────────────────────────────
        translate_ok = has_reasoning and has_fc and has_fco
        phase2_ok = p2_status == 200 and len(p2_text.strip()) > 0
        verdict_pass = translate_ok and phase2_ok and reflects_tool_result

        report["verdict"] = "PASS" if verdict_pass else "FAIL"
        report["phase1_http_status"] = p1_status
        report["phase1_stop_reason"] = phase1_json.get("stop_reason")
        report["phase2_stop_reason"] = data2.get("stop_reason")
        report["phase1_model"] = phase1_model
        report["counts"] = {
            "phase1_thinking_blocks": n_think1,
            "phase1_tool_use_blocks": n_tool1,
            "phase2_text_blocks": n_text2,
            "phase2_text_length": len(p2_text),
        }
        report["phase2_text_excerpt"] = _excerpt(p2_text, 200)
        report["reflects_tool_result"] = reflects_tool_result
        report["tool_result_markers_matched"] = matched
        report["phase2_excerpt_showing_tool_result"] = excerpt_reflect
        report["phase1_usage"] = {
            "input_tokens": p1_usage.get("input_tokens"),
            "output_tokens": p1_usage.get("output_tokens"),
        }
        report["phase2_usage"] = {
            "input_tokens": p2_usage.get("input_tokens"),
            "output_tokens": p2_usage.get("output_tokens"),
        }
        report["live_requests"] = _live_count

        print("\n=== R3 RESULT ===")
        print(json.dumps(report, indent=2))
        return 0 if verdict_pass else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
