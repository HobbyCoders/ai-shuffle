"""xdotool-style key-name parsing for the computer-use ``key`` tool.

The model emits chords like ``ctrl+c``, ``alt+Tab``, ``Return``, ``cmd+space``,
``Page_Down``. This maps those names to pynput keys so they can be pressed.
"""

from __future__ import annotations

from typing import Any


# Special key names -> pynput.keyboard.Key attribute name.
# Accepts xdotool names plus common aliases (case-insensitive lookup).
_SPECIAL = {
    "return": "enter", "enter": "enter", "kp_enter": "enter",
    "escape": "esc", "esc": "esc",
    "tab": "tab",
    "space": "space",
    "backspace": "backspace",
    "delete": "delete", "del": "delete",
    "insert": "insert",
    "home": "home", "end": "end",
    "page_up": "page_up", "pageup": "page_up", "prior": "page_up",
    "page_down": "page_down", "pagedown": "page_down", "next": "page_down",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "caps_lock": "caps_lock", "capslock": "caps_lock",
    "num_lock": "num_lock",
    "print": "print_screen", "printscreen": "print_screen", "snapshot": "print_screen",
    "menu": "menu",
    "pause": "pause",
    # modifiers
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt",
    "shift": "shift",
    "cmd": "cmd", "command": "cmd", "super": "cmd", "win": "cmd", "meta": "cmd",
}

_MODIFIERS = {"ctrl", "alt", "shift", "cmd"}

# Function keys F1..F20.
for _i in range(1, 21):
    _SPECIAL[f"f{_i}"] = f"f{_i}"


def _resolve(name: str) -> Any:
    """Resolve a single key token to a pynput key or a literal character.

    Returns a ``pynput.keyboard.Key`` for named keys, or a 1-char string for
    literal characters. Raises ValueError on unknown multi-char names.
    """
    from pynput.keyboard import Key

    token = name.strip()
    low = token.lower()
    if low in _SPECIAL:
        return getattr(Key, _SPECIAL[low])
    if len(token) == 1:
        return token
    raise ValueError(f"unknown key name: {name!r}")


def parse_modifiers(text: str) -> list[Any]:
    """Parse a modifier-only string like ``"ctrl+shift"`` into pynput keys.

    Used for the ``text`` field of click tools (modifiers held during a click).
    Empty/blank input returns ``[]``. Raises ValueError on a non-modifier token.
    """
    text = (text or "").strip()
    if not text:
        return []
    mods = []
    for tok in text.split("+"):
        tok = tok.strip()
        if not tok:
            continue
        if tok.lower() not in _MODIFIERS:
            raise ValueError(f"{tok!r} is not a modifier")
        mods.append(_resolve(tok))
    return mods


def parse_chord(text: str) -> tuple[list[Any], Any]:
    """Parse ``"ctrl+shift+t"`` into ``([Key.ctrl, Key.shift], 't')``.

    Returns ``(modifiers, main_key)``. ``main_key`` may be a pynput Key or a
    single character. Raises ValueError on empty/invalid input.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty key chord")

    # A bare "+" means the plus character itself.
    if text == "+":
        return ([], "+")

    parts = [p for p in text.split("+") if p != ""]
    if not parts:
        raise ValueError(f"invalid key chord: {text!r}")

    *mods, last = parts
    modifiers = []
    for m in mods:
        if m.strip().lower() not in _MODIFIERS:
            raise ValueError(f"{m!r} is not a modifier in chord {text!r}")
        modifiers.append(_resolve(m))
    return modifiers, _resolve(last)


def _key_name(token: str) -> str:
    """A chord token's canonical name: ``"Win"`` -> ``"cmd"``, ``"A"`` -> ``"a"``."""
    low = token.strip().lower()
    return _SPECIAL.get(low, low)


def is_system_chord(text: str) -> bool:
    """Whether ``text`` is a system key combination (the ``systemKeyCombos`` grant).

    System chords act on the desktop rather than the focused app: anything
    with the Windows/Command key (Start menu, Win+R, Win+L, window snapping),
    switching or closing apps (Alt+Tab, Alt+Esc, Alt+F4), the Start menu and
    Task Manager (Ctrl+Esc, Ctrl+Shift+Esc) and every Ctrl+Alt chord
    (Ctrl+Alt+Del style). Unparseable text counts as system: a refusal is the
    safe answer for a chord that cannot be classified.
    """
    raw = (text or "").strip()
    if raw == "+":
        return False
    parts = [p for p in raw.split("+") if p.strip() != ""]
    if not parts:
        return True
    names = [_key_name(p) for p in parts]
    *mods, main = names
    held = set(mods)
    if "cmd" in held or main == "cmd":
        return True
    if "alt" in held and main in ("tab", "esc", "f4"):
        return True
    if "ctrl" in held and main == "esc":
        return True
    return {"ctrl", "alt"} <= held
