"""The avatar allowlist is written three times; keep the copies identical."""
import re
from pathlib import Path

from app.db import assistant as store

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib"


def test_frontend_avatar_list_matches_backend_allowlist():
    source = (FRONTEND / "api" / "assistant.ts").read_text(encoding="utf-8")
    listed = re.search(r"export const AVATARS = \[(.*?)\] as const", source, re.S).group(1)
    assert tuple(re.findall(r"'([a-z]+)'", listed)) == store.AVATAR_PRESETS


def test_every_avatar_has_a_drawing():
    component = (FRONTEND / "components" / "assistant" / "AgentAvatar.svelte").read_text(encoding="utf-8")
    drawn = set(re.findall(r"avatar === '([a-z]+)'", component))
    # "robot" is the {:else} fallback branch.
    assert drawn | {"robot"} == set(store.AVATAR_PRESETS)
    assert len(store.AVATAR_PRESETS) == 16
