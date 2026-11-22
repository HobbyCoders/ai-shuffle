"""CDP screencast hub for the live browser panel.

One :class:`BrowserStreamHub` per session, created on the first viewer and torn
down after the last one leaves. The hub opens a single upstream CDP WebSocket to
the supervised Chromium page published in ``browser_panel_registry`` and drives
``Page.startScreencast``; each JPEG frame is fanned out to every connected
viewer through a per-viewer latest-frame-wins queue. Pure input translation is
used by the session-owned workspace broker; this hub never mutates a page.
Read-only point inspection may use the attached CDP socket after identity checks.

State machine (per hub, run by :meth:`BrowserStreamHub._run`):
  * no target registered            -> broadcast ``no_page``, wait for change
  * target registered               -> broadcast ``connecting`` -> attach ->
                                        first frame -> ``live``
  * registry change while attached   -> tear down upstream, re-evaluate target
  * upstream dies while attached     -> broadcast ``no_page``, wait for change
  * last viewer leaves               -> stopScreencast, close upstream, drop hub
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from contextlib import suppress
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import websockets
from fastapi import WebSocket
from pydantic import BaseModel, Field, StrictInt

from app.api.ws_utils import WSPayloadEncodeError, safe_ws_send
from app.core import browser_control, native_chrome
from app.core import browser_panel_registry as registry

logger = logging.getLogger(__name__)


# Screencast parameters — JPEG, moderate quality, capped dimensions, every frame.
_SCREENCAST_PARAMS = {
    "format": "jpeg",
    "quality": 60,
    "maxWidth": 1440,
    "maxHeight": 1440,
    "everyNthFrame": 1,
}

# Backoff schedule for re-attaching to a still-registered target after the
# upstream CDP socket dies. Roughly 17.5s of patience before falling back to
# no_page + wait-for-registry-change.
_REATTACH_DELAYS: Tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 5.0, 5.0)

# An attach that stayed up longer than this is considered to have been healthy:
# its eventual death starts a fresh retry budget instead of accumulating.
_ATTACH_HEALTHY_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Client input vocabulary + pure translation
# ---------------------------------------------------------------------------
class _MouseInput(BaseModel):
    """A client mouse event. Extra keys (``type``) are ignored."""

    kind: str
    event: str  # move | down | up | wheel
    x: float
    y: float
    button: str = "left"  # none | left | middle | right
    buttons: Optional[StrictInt] = Field(default=None, ge=0, le=31)
    clickCount: int = 0
    deltaX: float = 0.0
    deltaY: float = 0.0
    modifiers: int = 0


class _KeyInput(BaseModel):
    """A client key event. Extra keys (``type``) are ignored."""

    kind: str
    event: str  # down | up
    key: str = ""
    code: str = ""
    keyCode: int = 0
    text: str = ""
    modifiers: int = 0


class _NavigateInput(BaseModel):
    url: str


class _CommandInput(BaseModel):
    """A toolbar command: history walk, reload, or stop loading."""

    command: str  # back | forward | reload | stop


class _ViewportInput(BaseModel):
    """Live-view CSS size. Extra keys (``type``) are ignored."""

    width: int
    height: int


# Floor matches a usable live-view; ceiling tracks the screencast cap on
# height and a 1080p-class desktop width so a dragged-wide panel can cross
# the app's 1080px mobile cutoff.
_VIEWPORT_MIN = 200
_VIEWPORT_MAX_WIDTH = 1920
_VIEWPORT_MAX_HEIGHT = 1440


class InspectInput(BaseModel):
    """A point-and-prompt hit-test request at page CSS-pixel coordinates."""

    x: float
    y: float
    mode: str  # hover | select


_MOUSE_CDP_TYPE = {
    "move": "mouseMoved",
    "down": "mousePressed",
    "up": "mouseReleased",
    "wheel": "mouseWheel",
}
_VALID_MOUSE_BUTTONS = {"none", "left", "middle", "right"}


def _translate_mouse(m: _MouseInput, bounds: Optional[Tuple[int, int]]) -> dict:
    if m.event not in _MOUSE_CDP_TYPE:
        raise ValueError(f"invalid mouse event: {m.event!r}")
    if m.button not in _VALID_MOUSE_BUTTONS:
        raise ValueError(f"invalid mouse button: {m.button!r}")

    browser_control.finite({"x": m.x, "y": m.y, "deltaX": m.deltaX, "deltaY": m.deltaY})
    if (
        not 0 <= m.clickCount <= 3
        or not 0 <= m.modifiers <= 15
        or any(abs(v) > 100000 for v in (m.x, m.y, m.deltaX, m.deltaY))
    ):
        raise ValueError("Invalid mouse input bounds")
    x, y = float(m.x), float(m.y)
    if bounds is not None:
        w, h = bounds
        x = min(max(x, 0.0), float(w))
        y = min(max(y, 0.0), float(h))

    params: dict = {
        "type": _MOUSE_CDP_TYPE[m.event],
        "x": x,
        "y": y,
        "button": m.button,
        "clickCount": m.clickCount,
        "modifiers": m.modifiers,
    }
    if m.buttons is not None:
        params["buttons"] = m.buttons
    if m.event == "wheel":
        params["deltaX"] = m.deltaX
        params["deltaY"] = m.deltaY
    return {"method": "Input.dispatchMouseEvent", "params": params}


def _translate_key(k: _KeyInput) -> dict:
    if (
        not 0 <= k.modifiers <= 15
        or not 0 <= k.keyCode <= 65535
        or any(len(v) > 256 for v in (k.key, k.code, k.text))
    ):
        raise ValueError("Invalid key input bounds")
    if k.event not in ("down", "up"):
        raise ValueError(f"invalid key event: {k.event!r}")

    params: dict = {
        "type": "keyDown" if k.event == "down" else "keyUp",
        "key": k.key,
        "code": k.code,
        "windowsVirtualKeyCode": k.keyCode,
        "modifiers": k.modifiers,
    }
    if k.event == "down":
        text = k.text
        if k.key == "Enter":
            text = "\r"
        if text:
            params["text"] = text
    return {"method": "Input.dispatchKeyEvent", "params": params}


def _translate_navigate(msg: dict) -> dict:
    nav = _NavigateInput(**msg)
    scheme = urlparse(nav.url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"navigate url must be http(s): {nav.url!r}")
    return {"method": "Page.navigate", "params": {"url": nav.url}}


def _translate_command(msg: dict) -> dict:
    """Translate a toolbar command into its first CDP step.

    reload / stop map straight onto Page.* commands. back / forward need the
    page's navigation history first, so they return the ``getNavigationHistory``
    probe carrying a private ``__history_offset__`` marker; ``dispatch_input``
    consumes the marker and the pump loop completes the jump when the reply
    arrives (see :meth:`BrowserStreamHub._complete_history_jump`).
    """
    cmd = _CommandInput(**msg).command
    if cmd == "reload":
        return {"method": "Page.reload", "params": {"ignoreCache": False}}
    if cmd == "stop":
        return {"method": "Page.stopLoading"}
    if cmd in ("back", "forward"):
        return {
            "method": "Page.getNavigationHistory",
            "params": {},
            "__history_offset__": -1 if cmd == "back" else 1,
        }
    raise ValueError(f"invalid command: {cmd!r}")


def _translate_viewport(msg: dict) -> dict:
    """Map a panel-size update onto CDP device-metrics override.

    Headless Chrome's window does not follow the docked panel, so CSS
    ``@media (max-width: 1080px)`` would otherwise stay stuck at the
    launch viewport (or whatever the last agent ``browser_resize`` set).
    The live view's box is the viewport the user is looking at.
    """
    v = _ViewportInput(**msg)
    w, h = int(v.width), int(v.height)
    if w < _VIEWPORT_MIN or h < _VIEWPORT_MIN:
        raise ValueError(f"viewport too small: {w}x{h}")
    w = min(w, _VIEWPORT_MAX_WIDTH)
    h = min(h, _VIEWPORT_MAX_HEIGHT)
    return {
        "method": "Emulation.setDeviceMetricsOverride",
        "params": {
            "width": w,
            "height": h,
            "deviceScaleFactor": 1,
            "mobile": False,
        },
    }


def translate_input(
    msg: dict, bounds: Optional[Tuple[int, int]] = None
) -> Optional[dict]:
    """Translate one client message into a CDP command dict (or None).

    Pure and side-effect free. ``bounds`` is the last frame's
    ``(deviceWidth, deviceHeight)`` used to clamp mouse coordinates. Raises
    ``ValueError``/``ValidationError`` on any invalid or unknown message — the
    caller treats that as a protocol violation.
    """
    if msg.get("type") == "input" and msg.get("kind") == "text":
        text = msg.get("text")
        if not isinstance(text, str) or not 0 < len(text) <= 16_384:
            raise ValueError("text input must contain 1 to 16384 characters")
    browser_control.finite(msg)
    mtype = msg.get("type")
    if mtype == "navigate":
        return _translate_navigate(msg)
    if mtype == "command":
        return _translate_command(msg)
    if mtype == "viewport":
        return _translate_viewport(msg)
    if mtype == "input":
        kind = msg.get("kind")
        if kind == "mouse":
            return _translate_mouse(_MouseInput(**msg), bounds)
        if kind == "key":
            return _translate_key(_KeyInput(**msg))
        if kind == "text":
            text = msg.get("text")
            if not isinstance(text, str) or not 0 < len(text) <= 16_384:
                raise ValueError("text input must contain 1 to 16384 characters")
            return {"method": "Input.insertText", "params": {"text": text}}
        raise ValueError(f"invalid input kind: {kind!r}")
    raise ValueError(f"unknown client message type: {mtype!r}")


# ---------------------------------------------------------------------------
# Inspect (point-and-prompt element hit-test)
# ---------------------------------------------------------------------------
_VALID_INSPECT_MODES = {"hover", "select"}

# Upper bound on the JSON-encoded element descriptor a page may hand back. The
# in-page script caps every field itself, but the page owns its own JS realm,
# so the server re-checks before fanning anything out to a viewer.
_MAX_INSPECT_RESULT_BYTES = 32_768

# Self-contained hit-test script evaluated in the page. Placeholders __X__ /
# __Y__ / __DETAIL__ are substituted with JSON-encoded values. Returns either
# null or a plain-JSON element descriptor (returnByValue serialisable).
_INSPECT_JS_TEMPLATE = """
(() => {
  const el = document.elementFromPoint(__X__, __Y__);
  if (!el || el.nodeType !== 1) return null;
  const cap = (s, n) =>
    typeof s === 'string' && s.length > n ? s.slice(0, n) + '…' : s;
  const path = [];
  let cur = el;
  while (cur && cur.nodeType === 1 && path.length < 8) {
    let seg = cur.tagName.toLowerCase();
    if (cur.id) {
      path.unshift(seg + '#' + CSS.escape(cur.id));
      break;
    }
    const parent = cur.parentElement;
    if (parent) {
      const same = Array.from(parent.children).filter(
        (c) => c.tagName === cur.tagName,
      );
      if (same.length > 1) seg += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
    }
    path.unshift(seg);
    cur = parent;
  }
  const r = el.getBoundingClientRect();
  const out = {
    selector: path.join(' > '),
    tag: el.tagName.toLowerCase(),
    id: el.id || null,
    classes: Array.from(el.classList).slice(0, 12).map((c) => cap(c, 80)),
    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
  };
  if (__DETAIL__) {
    out.role = el.getAttribute('role');
    out.text = cap(
      ((el.innerText || el.textContent || '') + '').trim().replace(/\\s+/g, ' '),
      200,
    );
    // Without the marker a design preview puts on the elements it styles.
    out.html = cap(el.outerHTML.replace(/ data-ais-design="[c0-9 ]*"/g, ''), 800);
    const attrs = {};
    for (const a of Array.from(el.attributes).filter((a) => a.name !== 'data-ais-design').slice(0, 12)) {
      attrs[cap(a.name, 60)] = cap(a.value, 160);
    }
    out.attrs = attrs;
    // Design mode detail. Every block is best-effort: a hostile or odd page
    // must never turn a selection into a miss.
    try {
      __REGISTRY__
      let key = null;
      for (const [k, v] of reg.els) if (v === el) { key = k; break; }
      if (!key) {
        if (reg.els.size >= 200) reg.els.delete(reg.els.keys().next().value);
        // A key a queued change rebound after a reload is never handed out again.
        do { key = 'k' + (++reg.n); } while (reg.els.has(key));
        reg.els.set(key, el);
      }
      out.key = key;
    } catch (e) {}
    try {
      out.label = cap(
        (el.getAttribute('aria-label') || el.getAttribute('alt') ||
          el.getAttribute('placeholder') || el.getAttribute('title') || '') + '',
        120,
      );
    } catch (e) {}
    try {
      const cs = getComputedStyle(el);
      const styles = {};
      for (const p of [
        'font-size', 'font-weight', 'line-height', 'font-family', 'color',
        'background-color', 'padding-top', 'padding-right', 'padding-bottom',
        'padding-left', 'margin-bottom', 'border-top-left-radius', 'display', 'gap',
      ]) styles[p] = cap(cs.getPropertyValue(p), 80);
      out.styles = styles;
    } catch (e) {}
    try {
      let similar = 1;
      if (el.classList.length) {
        similar = document.querySelectorAll(
          el.tagName.toLowerCase() +
            Array.from(el.classList).slice(0, 6).map((c) => '.' + CSS.escape(c)).join(''),
        ).length;
      }
      out.similar = Math.min(similar, 999);
    } catch (e) {}
    try {
      // Where the element comes from, when the page is a framework dev build.
      let node = el;
      for (let i = 0; node && i < 6 && !out.source; i++, node = node.parentElement) {
        const sm = node.__svelte_meta;
        if (sm && sm.loc && sm.loc.file) {
          out.source = { file: cap(sm.loc.file + '', 200), line: sm.loc.line | 0 };
          break;
        }
        const fiberKey = Object.keys(node).find((k) => k.startsWith('__reactFiber$'));
        if (fiberKey) {
          let f = node[fiberKey];
          for (let j = 0; f && j < 12; j++, f = f.return) {
            if (f._debugSource && f._debugSource.fileName) {
              out.source = {
                file: cap(f._debugSource.fileName + '', 200),
                line: f._debugSource.lineNumber | 0,
              };
              break;
            }
          }
        }
        const vue = node.__vueParentComponent;
        if (!out.source && vue && vue.type && vue.type.__file) {
          out.source = { file: cap(vue.type.__file + '', 200), line: 0 };
        }
      }
    } catch (e) {}
    try {
      // The page's own colour variables, so a tweak can stay on its palette.
      const tokens = [];
      const seen = new Set();
      const rootCs = getComputedStyle(document.documentElement);
      let budget = 3000;
      const walk = (rules) => {
        for (const rule of Array.from(rules)) {
          if (budget-- <= 0 || tokens.length >= 24) return;
          if (rule.cssRules && !rule.selectorText) { walk(rule.cssRules); continue; }
          if (!rule.style || !/(^|,)\\s*(:root|html|body)(\\s*(,|$)|[.:\\[])/.test(rule.selectorText || '')) continue;
          for (const name of Array.from(rule.style)) {
            if (!name.startsWith('--') || seen.has(name)) continue;
            const v = rootCs.getPropertyValue(name).trim();
            if (!v || v.length > 60 || !CSS.supports('color', v)) continue;
            seen.add(name);
            tokens.push({ name: cap(name, 60), value: v });
            if (tokens.length >= 24) return;
          }
        }
      };
      for (const sheet of Array.from(document.styleSheets).slice(0, 40)) {
        let rules = null;
        try { rules = sheet.cssRules; } catch (e) { continue; }
        if (rules) walk(rules);
      }
      out.tokens = tokens;
    } catch (e) {}
  }
  return out;
})()
"""

# ---------------------------------------------------------------------------
# Design mode (live style preview on the inspected page)
# ---------------------------------------------------------------------------
# A design request previews a change on the page without touching source: it
# styles an element picked through inspect, reports where the picked elements
# are now, or puts the page back. The agent makes the real edit in code; this
# is only the "try it first" step.
#
# The page keeps one record per queued change (per viewer and element key).
# A preview never writes to an element's own style: each change is a rule in
# one stylesheet the script adds to the page, and its elements carry a marker
# attribute. So changes that overlap on an element never erase one another,
# removing one leaves the rest, and putting the page back is deleting the
# rules and the markers. The scripts are exercised against a DOM in
# frontend/src/lib/browser-panel/design-page-script.test.ts.
_VALID_DESIGN_OPS = {"style", "rects", "reset", "peek"}
_DESIGN_PROPS = frozenset({
    "font-size", "font-weight", "color", "background-color", "padding",
    "border-radius", "margin-bottom", "gap", "line-height", "opacity",
})
_DESIGN_KEY_RE = re.compile(r"^k[0-9]{1,7}$")
# Plain CSS values only: no url(), no declaration or rule breakouts, no quotes.
_DESIGN_VALUE_RE = re.compile(r"^[#%(),./\w\s-]{1,64}$")
_MAX_DESIGN_ITEMS = 40

# Shared by the inspect and design scripts. Keys start at a random number per
# document so a key from before a reload does not name a different element.
_DESIGN_REGISTRY_JS = """const S = Symbol.for('ais.design');
      const reg = window[S] || (window[S] = {
        n: Math.floor(Math.random() * 9000000), els: new Map(),
        changes: new Map(), hidden: new Set(), seq: 0, sheet: null, roots: new Set(),
      });"""

_DESIGN_JS_TEMPLATE = """
(() => {
  const a = __ARGS__;
  __REGISTRY__
  const MARK = 'data-ais-design';
  const find = (key, selector) => {
    let el = key ? reg.els.get(key) : null;
    if (el && !el.isConnected) el = null;
    if (!el && selector) {
      try { el = document.querySelector(selector); } catch (e) { el = null; }
      if (el && key) reg.els.set(key, el);
    }
    return el || null;
  };
  const rectOf = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height };
  };
  // Every element sharing the picked one's tag and classes, the picked one included.
  const peers = (el) => {
    if (!el.classList.length) return [el];
    try {
      const all = Array.from(document.querySelectorAll(
        el.tagName.toLowerCase() +
          Array.from(el.classList).slice(0, 6).map((c) => '.' + CSS.escape(c)).join(''),
      )).slice(0, 999);
      if (!all.includes(el)) {
        if (all.length >= 999) all.pop();
        all.push(el);
      }
      return all;
    } catch (e) { return [el]; }
  };
  const targetsOf = (owner, into) => {
    for (const c of reg.changes.values()) {
      if (owner === null || c.owner === owner) for (const t of c.targets) into.add(t);
    }
    return into;
  };
  // A preview never touches an element's own style. Each change is one rule
  // in a stylesheet this script owns, and the elements it applies to carry
  // the change's token in MARK. Taking a preview off is deleting the rule and
  // the token, so there is nothing of the page's to save or put back.
  // The rule outweighs the page's own rules and its normal inline styles. It
  // does not outweigh an inline !important declaration or an !important one
  // in a cascade layer: there the preview does not show, and the page is
  // still left as it was.
  const render = (els) => {
    const tokens = new Map();
    const roots = new Set();
    const live = new Set();
    for (const c of reg.changes.values()) {
      live.add(c.token);
      for (const t of c.targets) {
        if (!t.isConnected) continue;
        const root = t.getRootNode();
        if (root && root.adoptedStyleSheets) roots.add(root);
        if (!els.has(t)) continue;
        if (!tokens.has(t)) tokens.set(t, []);
        tokens.get(t).push(c.token);
      }
    }
    for (const el of els) {
      const mine = tokens.get(el);
      if (mine) el.setAttribute(MARK, mine.join(' '));
      else el.removeAttribute(MARK);
    }
    if (reg.changes.size && !reg.sheet) reg.sheet = new CSSStyleSheet();
    if (reg.sheet) {
      reg.sheet.replaceSync('');
      for (const c of reg.changes.values()) {
        if (reg.hidden.has(c.owner)) continue;
        // :is() lends the rule the weight of four ids without matching on
        // them. Each value is set on its own, so one the browser does not
        // accept is dropped and cannot spill into another change's rule.
        const at = reg.sheet.insertRule(
          '[' + MARK + '~="' + c.token + '"]:is(*, #a#a#a#a) {}', reg.sheet.cssRules.length);
        const rule = reg.sheet.cssRules[at];
        for (const [prop, value] of Object.entries(c.styles)) {
          rule.style.setProperty(prop, value, 'important');
        }
      }
    }
    // The sheet sits last in each tree that has a marked element, and leaves
    // with the last change.
    for (const root of new Set([...reg.roots, ...roots])) {
      const others = Array.from(root.adoptedStyleSheets).filter((sheet) => sheet !== reg.sheet);
      const want = reg.changes.size && roots.has(root);
      const has = others.length !== root.adoptedStyleSheets.length;
      const last = root.adoptedStyleSheets[root.adoptedStyleSheets.length - 1] === reg.sheet;
      if (want ? !last : has) root.adoptedStyleSheets = want ? [...others, reg.sheet] : others;
      // A copy the page made of a marked element keeps no token of a change
      // that is gone.
      for (const el of root.querySelectorAll('[' + MARK + ']')) {
        const mine = (el.getAttribute(MARK) || '').split(' ').filter((token) => live.has(token));
        if (!mine.length) el.removeAttribute(MARK);
        else if (mine.join(' ') !== el.getAttribute(MARK)) el.setAttribute(MARK, mine.join(' '));
      }
    }
    reg.roots = reg.changes.size ? roots : new Set();
    if (!reg.changes.size) reg.sheet = null;
  };
  if (a.op === 'rects') {
    const rects = {};
    for (const item of a.items) {
      const el = find(item.key, item.selector);
      rects[item.key] = el ? rectOf(el) : null;
    }
    return { rects };
  }
  if (a.op === 'peek') {
    // Show the page as it really is, without dropping the queued styles.
    if (a.on) reg.hidden.add(a.owner);
    else reg.hidden.delete(a.owner);
    render(targetsOf(a.owner, new Set()));
    return { peek: reg.hidden.has(a.owner) };
  }
  if (a.op === 'reset') {
    // One change (owner and key), one viewer's changes (owner), or all of them.
    const els = new Set();
    const id = a.key ? a.owner + ':' + a.key : null;
    for (const [cid, c] of Array.from(reg.changes)) {
      if (id ? cid !== id : a.owner !== null && c.owner !== a.owner) continue;
      for (const t of c.targets) els.add(t);
      reg.changes.delete(cid);
    }
    if (!id) {
      if (a.owner === null) reg.hidden.clear();
      else reg.hidden.delete(a.owner);
    }
    render(els);
    return { reset: true };
  }
  // op === 'style': `styles` is the whole preview for this change, so a
  // property that is no longer listed goes back to what the page had.
  const el = find(a.key, a.selector);
  if (!el) return { key: a.key, missing: true };
  const id = a.owner + ':' + a.key;
  const before = reg.changes.get(id);
  if (!before && reg.changes.size >= 200) return { key: a.key, missing: true };
  const els = new Set(before ? before.targets : []);
  // Trying a value shows this viewer's changes again if they were hidden.
  if (reg.hidden.delete(a.owner)) targetsOf(a.owner, els);
  const targets = a.scope === 'all' ? peers(el) : [el];
  for (const t of targets) els.add(t);
  if (!Object.keys(a.styles).length) reg.changes.delete(id);
  else if (before) { before.targets = targets; before.styles = a.styles; }
  else reg.changes.set(id, { owner: a.owner, targets, styles: a.styles, token: 'c' + (++reg.seq) });
  render(els);
  return { key: a.key, rect: rectOf(el), count: targets.length };
})()
"""


class DesignInput(BaseModel):
    """One design-mode request. Unknown keys (``type``, identity) are ignored."""

    op: str
    key: Optional[str] = None
    selector: Optional[str] = None
    scope: Optional[str] = None
    styles: Optional[Dict[str, str]] = None
    items: Optional[List[Dict[str, Optional[str]]]] = None
    on: Optional[bool] = None


def _design_target(key: object, selector: object) -> Tuple[str, Optional[str]]:
    if not isinstance(key, str) or not _DESIGN_KEY_RE.match(key):
        raise ValueError("invalid design element key")
    if selector is not None and (not isinstance(selector, str) or len(selector) > 500):
        raise ValueError("invalid design selector")
    return key, selector


def parse_design(msg: dict) -> dict:
    """Validate one client ``design`` message into the page script's arguments."""
    design = DesignInput(**msg)
    if design.op not in _VALID_DESIGN_OPS:
        raise ValueError(f"invalid design op: {design.op!r}")
    if design.op == "rects":
        items = design.items or []
        if len(items) > _MAX_DESIGN_ITEMS:
            raise ValueError("too many design elements")
        out = []
        for item in items:
            key, selector = _design_target(item.get("key"), item.get("selector"))
            out.append({"key": key, "selector": selector})
        return {"op": "rects", "items": out}
    if design.op == "peek":
        return {"op": "peek", "on": bool(design.on)}
    if design.op == "reset":
        if design.key is None:
            return {"op": "reset", "key": None}
        key, _ = _design_target(design.key, None)
        return {"op": "reset", "key": key}
    key, selector = _design_target(design.key, design.selector)
    if design.scope not in ("one", "all"):
        raise ValueError(f"invalid design scope: {design.scope!r}")
    styles = design.styles or {}
    for prop, value in styles.items():
        if prop not in _DESIGN_PROPS:
            raise ValueError(f"design property not allowed: {prop!r}")
        if not _DESIGN_VALUE_RE.match(value) or "url(" in value.lower():
            raise ValueError(f"invalid design value for {prop}")
    return {
        "op": "style",
        "key": key,
        "selector": selector,
        "scope": design.scope,
        "styles": styles,
    }


def build_design_expression(args: dict, owner: Optional[str] = None) -> str:
    """Build the page-side expression for one validated design request.

    ``owner`` names the viewer whose changes the request touches. It is chosen
    by the server, never read from the client. ``None`` is the server's own
    "every viewer" reset.
    """
    return _DESIGN_JS_TEMPLATE.replace("__REGISTRY__", _DESIGN_REGISTRY_JS).replace(
        "__ARGS__", json.dumps({**args, "owner": owner})
    )


def parse_inspect(msg: dict) -> InspectInput:
    """Validate one client ``inspect`` message. Raises on anything invalid."""
    inspect = InspectInput(**msg)
    if inspect.mode not in _VALID_INSPECT_MODES:
        raise ValueError(f"invalid inspect mode: {inspect.mode!r}")
    if not (_is_finite(inspect.x) and _is_finite(inspect.y)):
        raise ValueError("inspect coordinates must be finite")
    return inspect


def _is_finite(v: float) -> bool:
    return v == v and v not in (float("inf"), float("-inf"))


def build_inspect_expression(
    x: float, y: float, detail: bool, bounds: Optional[Tuple[int, int]] = None
) -> str:
    """Build the page-side hit-test expression for one inspect request."""
    if bounds is not None:
        w, h = bounds
        x = min(max(x, 0.0), float(w))
        y = min(max(y, 0.0), float(h))
    return (
        _INSPECT_JS_TEMPLATE.replace("__X__", json.dumps(float(x)))
        .replace("__Y__", json.dumps(float(y)))
        .replace("__DETAIL__", "true" if detail else "false")
        .replace("__REGISTRY__", _DESIGN_REGISTRY_JS)
    )


def sanitize_inspect_result(value: object) -> Optional[dict]:
    """Server-side guard on the page-provided descriptor: dict + bounded size."""
    if not isinstance(value, dict):
        return None
    try:
        encoded = json.dumps(value)
    except (TypeError, ValueError):
        return None
    if len(encoded) > _MAX_INSPECT_RESULT_BYTES:
        return None
    return value


# ---------------------------------------------------------------------------
# Upstream connection seam (patched in tests)
# ---------------------------------------------------------------------------
async def _open_upstream(uri: str):
    """Open the upstream CDP WebSocket. Overridden in tests with a stub."""
    return await websockets.connect(uri, max_size=None, open_timeout=5)


# ---------------------------------------------------------------------------
# Internal control-flow signals
# ---------------------------------------------------------------------------
class _RegistryChanged(Exception):
    """Raised inside the pump loop when the registry target changed."""


# ---------------------------------------------------------------------------
# Viewer
# ---------------------------------------------------------------------------
class _Viewer:
    """One connected panel WebSocket.

    Frames and page state coalesce separately on ``queue`` (at most two
    messages, latest of each type) and are drained by a
    dedicated sender task. ``send_lock`` serialises every write to the socket so
    the frame sender, status broadcasts and pings never interleave.
    """

    __slots__ = (
        "websocket", "queue", "sender_task", "send_lock", "redact_urls",
        "design_owner", "design_active",
    )

    def __init__(self, websocket: WebSocket, *, redact_urls: bool = False) -> None:
        self.websocket = websocket
        self.redact_urls = redact_urls
        # Names this viewer's design previews on the page, so they can be put
        # back when it leaves. ``design_active``: it has previews on the page.
        self.design_owner = secrets.token_hex(6)
        self.design_active = False
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        self.sender_task: Optional[asyncio.Task] = None
        self.send_lock = asyncio.Lock()

    def enqueue(self, msg: dict) -> None:
        """Replace only the same message type, without awaiting a slow socket."""
        retained = []
        while not self.queue.empty():
            pending = self.queue.get_nowait()
            if pending["type"] != msg["type"]:
                retained.append(pending)
        for pending in retained:
            self.queue.put_nowait(pending)
        self.queue.put_nowait(msg)

    def discard_frame(self) -> None:
        retained = []
        while not self.queue.empty():
            pending = self.queue.get_nowait()
            if pending["type"] != "frame":
                retained.append(pending)
        for pending in retained:
            self.queue.put_nowait(pending)


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------
class BrowserStreamHub:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.viewers: Set[_Viewer] = set()
        self._task: Optional[asyncio.Task] = None
        self._upstream = None
        self._upstream_msg_id = 0
        self._last_metadata: Optional[dict] = None
        self._last_frame: Optional[dict] = None
        self._last_frame_key = None
        # Last status broadcast to viewers (None until the hub reports one).
        self.status: Optional[dict] = None
        self._attached_target_id = None
        self._frame_identity = {}
        # Toolbar state tracked from CDP page events, mirrored to viewers as
        # ``page`` messages. msg-id -> history offset awaiting its reply.
        self._pending_jumps: Dict[int, int] = {}
        # msg-id -> (viewer, mode) awaiting a Runtime.evaluate hit-test reply.
        self._pending_inspects: Dict[int, Tuple["_Viewer", str, dict]] = {}
        self.page_url: Optional[str] = None
        self.page_loading: bool = False
        self._loading_frames: set[str] = set()
        # Monotonic time the current/last upstream attach opened, for the
        # healthy-attach check in the reattach backoff. None = never opened.
        self._attach_opened_at: Optional[float] = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._clear_frame()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for viewer in list(self.viewers):
            await self._cancel_sender(viewer)
        self.viewers.clear()

    def viewer_count(self) -> int:
        return len(self.viewers)

    # -- viewers -------------------------------------------------------------
    async def add_viewer(
        self, websocket: WebSocket, *, redact_urls: bool = False
    ) -> _Viewer:
        viewer = _Viewer(websocket, redact_urls=redact_urls)
        self.viewers.add(viewer)
        if self._last_frame is not None:
            if self._last_frame_key == self._current_frame_key():
                viewer.enqueue(self._last_frame)
            else:
                self._clear_frame()
        viewer.sender_task = asyncio.create_task(self._viewer_sender(viewer))
        return viewer

    async def remove_viewer(self, viewer: _Viewer) -> None:
        self.viewers.discard(viewer)
        await self._cancel_sender(viewer)
        # A panel that closed or lost its connection cannot put the page back
        # itself, so its previews end here.
        if viewer.design_active:
            viewer.design_active = False
            await self._reset_design(self._upstream, viewer.design_owner)

    async def _reset_design(self, ws, owner: Optional[str]) -> None:
        """Take design previews off the page: one viewer's, or all (``None``)."""
        if ws is None:
            return
        await self._send_upstream(
            ws,
            "Runtime.evaluate",
            {
                "expression": build_design_expression(
                    {"op": "reset", "key": None}, owner
                ),
                "returnByValue": True,
            },
        )

    async def _cancel_sender(self, viewer: _Viewer) -> None:
        if viewer.sender_task is not None:
            viewer.sender_task.cancel()
            with suppress(asyncio.CancelledError):
                await viewer.sender_task
            viewer.sender_task = None

    async def _viewer_sender(self, viewer: _Viewer) -> None:
        try:
            while True:
                msg = await viewer.queue.get()
                ok = await self.send_to_viewer(viewer, msg)
                if not ok:
                    break
        except asyncio.CancelledError:
            pass

    async def send_to_viewer(self, viewer: _Viewer, msg: dict) -> bool:
        """Serialised write to one viewer. Returns False on a dead connection."""
        if viewer.redact_urls and "url" in msg:
            msg = {**msg, "url": None}
        async with viewer.send_lock:
            if msg.get("type") == "frame":
                if self._last_frame_key != self._current_frame_key():
                    self._clear_frame()
                if msg is not self._last_frame:
                    return True
            try:
                await safe_ws_send(viewer.websocket, msg)
                return True
            except WSPayloadEncodeError:
                return True  # bad single message, connection still healthy
            except Exception:
                return False

    async def _broadcast_status(self, state: str, url: Optional[str]) -> None:
        msg = {"type": "status", "state": state, "url": url}
        # Kept for a viewer that joins later: nothing repeats "live" on a page
        # that sits still, so without it a second viewer waits on "connecting".
        self.status = msg
        for viewer in list(self.viewers):
            await self.send_to_viewer(viewer, msg)

    async def repeat_status(self, viewer: _Viewer) -> None:
        """Tell one viewer the state the others already have.

        The status is read under the viewer's send lock, so a broadcast that
        is waiting for the lock cannot be followed by an older state.
        """
        async with viewer.send_lock:
            msg = self.status
            if msg is None:
                return
            if viewer.redact_urls:
                msg = {**msg, "url": None}
            with suppress(Exception):
                await safe_ws_send(viewer.websocket, msg)

    def _broadcast_page(self) -> None:
        """Fan out toolbar page state (url + loading), latest-wins, never blocks."""
        msg = {"type": "page", "url": self.page_url, "loading": self.page_loading}
        for viewer in list(self.viewers):
            viewer.enqueue(msg)

    def _handle_page_event(self, msg: dict) -> bool:
        """Update tracked page state from a CDP event. True if state changed."""
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "Page.frameStartedLoading":
            self._loading_frames.add(params.get("frameId", ""))
            self.page_loading = True
            return True
        if method == "Page.frameStoppedLoading":
            self._loading_frames.discard(params.get("frameId", ""))
            self.page_loading = bool(self._loading_frames)
            return True
        if method == "Page.frameDetached":
            self._loading_frames.discard(params.get("frameId", ""))
            self.page_loading = bool(self._loading_frames)
            return True
        if method == "Page.frameNavigated":
            frame = params.get("frame", {})
            # Main frames carry no parentId (or an absent/null one).
            if frame.get("parentId") is not None:
                return False
            url = frame.get("url")
            if url is not None and url != self.page_url:
                self.page_url = url
                return True
        if method == "Page.navigatedWithinDocument":
            url = params.get("url")
            if url is not None and url != self.page_url:
                self.page_url = url
                return True
        return False

    def _current_frame_key(self) -> tuple:
        """Identity only: URL/loading changes must not invalidate a static frame."""
        target = registry.get_target(self.session_id)
        broker = browser_control._brokers.get(self.session_id)
        return (
            target.target_id if target else None,
            target.tab_id if target else None,
            broker.runtime_id if broker else None,
            broker.state["active_tab_id"] if broker else None,
            self._attached_target_id,
            self._frame_identity.get("runtime_id"),
            self._frame_identity.get("tab_id"),
        )

    def _clear_frame(self) -> None:
        self._last_frame = None
        self._last_frame_key = None
        self._last_metadata = None
        for viewer in self.viewers:
            viewer.discard_frame()

    def _fan_out_frame(self, data: str, metadata: dict) -> None:
        """Enqueue one shared frame to every viewer, latest-wins. Never blocks."""
        broker = browser_control._brokers.get(self.session_id)
        target = registry.get_target(self.session_id)
        if self._frame_identity and (
            not broker
            or broker.runtime_id != self._frame_identity["runtime_id"]
            or broker.state["active_tab_id"] != self._frame_identity["tab_id"]
        ):
            self._clear_frame()
            return
        if self._attached_target_id is not None and (
            target is None
            or target.target_id != self._attached_target_id
            or (
                self._frame_identity and target.tab_id != self._frame_identity["tab_id"]
            )
        ):
            self._clear_frame()
            return
        if not data:
            return
        msg = {
            "type": "frame",
            "data": data,
            "metadata": {**metadata, **self._frame_identity},
        }
        self._last_frame = msg
        self._last_frame_key = self._current_frame_key()
        self._last_metadata = metadata
        for viewer in list(self.viewers):
            viewer.enqueue(msg)

    # -- input dispatch ------------------------------------------------------
    def last_bounds(self) -> Optional[Tuple[int, int]]:
        md = self._last_metadata
        if not md:
            return None
        w = md.get("deviceWidth")
        h = md.get("deviceHeight")
        if w and h:
            return (int(w), int(h))
        return None

    async def ensure_target(self, url: str) -> bool:
        """Viewing must never create a second page authority."""
        raise ValueError("Browser mutations require the session workspace runtime")

    async def dispatch_input(self, cdp_cmd: dict) -> None:
        raise ValueError("Browser mutations require the session workspace runtime")

    async def dispatch_inspect(self, viewer: "_Viewer", inspect: InspectInput) -> None:
        """Run one point-and-prompt hit-test for ``viewer``.

        Sends ``Runtime.evaluate`` with the in-page hit-test script; the pump
        loop pairs the reply back through ``_pending_inspects`` and answers the
        requesting viewer with an ``inspect-hit`` frame. With no upstream page
        attached the viewer gets an immediate empty hit so its hover/selection
        state clears instead of hanging.
        """
        ws = self._upstream
        if ws is None:
            await self.send_to_viewer(
                viewer, {"type": "inspect-hit", "mode": inspect.mode, "element": None}
            )
            return
        expression = build_inspect_expression(
            inspect.x,
            inspect.y,
            detail=inspect.mode == "select",
            bounds=self.last_bounds(),
        )
        if not await self._send_upstream(
            ws,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            response=True,
        ):
            return
        if len(self._pending_inspects) >= 128:
            self._pending_inspects.pop(next(iter(self._pending_inspects)))
        self._pending_inspects[self._upstream_msg_id] = (
            viewer,
            inspect.mode,
            dict(self._frame_identity),
        )

    async def _complete_inspect(self, msg_id: int, reply: dict) -> None:
        """Answer the viewer that asked for the hit-test (if still connected)."""
        pending = self._pending_inspects.pop(msg_id, None)
        if pending is None:
            return
        viewer, mode, identity = pending
        broker = browser_control._brokers.get(self.session_id)
        if identity and (
            not broker
            or broker.runtime_id != identity["runtime_id"]
            or broker.state["active_tab_id"] != identity["tab_id"]
        ):
            return
        if viewer not in self.viewers:
            return
        value = reply.get("result", {}).get("value")
        if mode.startswith("design:"):
            await self.send_to_viewer(
                viewer,
                {
                    "type": "design-result",
                    "op": mode[len("design:"):],
                    "result": sanitize_inspect_result(value),
                },
            )
            return
        element = sanitize_inspect_result(value)
        await self.send_to_viewer(
            viewer, {"type": "inspect-hit", "mode": mode, "element": element}
        )

    async def dispatch_design(self, viewer: "_Viewer", args: dict) -> None:
        """Run one design-mode preview request for ``viewer``.

        Same request and reply pairing as a hit-test: the page script runs
        through ``Runtime.evaluate`` and the pump loop answers the requesting
        viewer with a ``design-result`` frame. It only ever adds or removes
        preview rules for elements already picked through inspect.
        """
        ws = self._upstream
        op = args["op"]
        if ws is None:
            await self.send_to_viewer(
                viewer, {"type": "design-result", "op": op, "result": None}
            )
            return
        if not await self._send_upstream(
            ws,
            "Runtime.evaluate",
            {
                "expression": build_design_expression(args, viewer.design_owner),
                "returnByValue": True,
            },
            response=True,
        ):
            return
        if op == "style":
            viewer.design_active = True
        elif op == "reset" and args.get("key") is None:
            viewer.design_active = False
        if len(self._pending_inspects) >= 128:
            self._pending_inspects.pop(next(iter(self._pending_inspects)))
        self._pending_inspects[self._upstream_msg_id] = (
            viewer,
            f"design:{op}",
            dict(self._frame_identity),
        )

    async def _complete_history_jump(self, msg_id: int, reply: dict) -> None:
        """Finish a back/forward command once getNavigationHistory replies.

        Offset 0 marks the attachment-time history probe: seed the tracked URL
        from the current entry instead of navigating.
        """
        offset = self._pending_jumps.pop(msg_id, None)
        if offset is None:
            return
        entries = reply.get("entries", [])
        index = reply.get("currentIndex", 0)
        if not 0 <= index < len(entries):
            return
        current_url = entries[index].get("url")
        if offset == 0:
            if current_url and current_url != self.page_url:
                self.page_url = current_url
                self._broadcast_page()
            return
        # Nonzero history offsets are never dispatched by the viewing hub.

    async def _send_upstream(
        self, ws, method: str, params: Optional[dict] = None, *, response: bool = False
    ) -> bool:
        """Send one CDP command. Returns False on a dead upstream socket."""
        try:
            self._upstream_msg_id += 1
            payload: dict = {"id": self._upstream_msg_id, "method": method}
            if params is not None:
                payload["params"] = params
            await ws.send(json.dumps(payload))
            return True
        except Exception:
            return False

    # -- state machine -------------------------------------------------------
    async def _run(self) -> None:
        try:
            failures = 0
            while True:
                target = registry.get_target(self.session_id)
                if target is None or not target.target_id:
                    await self._broadcast_status("no_page", None)
                    await registry.wait_for_change(self.session_id)
                    continue

                await self._broadcast_status("connecting", target.url)
                try:
                    await self._attach_and_pump(target)
                except _RegistryChanged:
                    failures = 0
                    continue
                except Exception:
                    logger.debug(
                        "browser panel upstream ended for %s",
                        self.session_id,
                        exc_info=True,
                    )
                    # An upstream hiccup while the registry STILL points at this
                    # target is not "no page": the agent's browser tools only
                    # re-report on tab create/close/switch, so waiting for a
                    # registry change here left the panel dead for the rest of
                    # the turn (the mid-stream "reconnecting forever" bug).
                    # Retry the same target with backoff, racing a registry
                    # change; only after the retry budget is spent do we fall
                    # back to no_page + wait. A connection that had been healthy
                    # for a while earns a fresh budget; an instant-death target
                    # accumulates failures until the budget runs out.
                    if self._attach_alive_for() > _ATTACH_HEALTHY_SECONDS:
                        failures = 1
                    else:
                        failures += 1
                    current = registry.get_target(self.session_id)
                    same_target = (
                        current is not None and current.target_id == target.target_id
                    )
                    if same_target and failures <= len(_REATTACH_DELAYS):
                        delay = _REATTACH_DELAYS[failures - 1]
                        change = asyncio.create_task(
                            registry.wait_for_change(self.session_id)
                        )
                        done, _ = await asyncio.wait({change}, timeout=delay)
                        if not done:
                            change.cancel()
                            with suppress(asyncio.CancelledError, Exception):
                                await change
                        continue
                    failures = 0
                    await self._broadcast_status("no_page", None)
                    await registry.wait_for_change(self.session_id)
                    continue
        except asyncio.CancelledError:
            pass

    def _attach_alive_for(self) -> float:
        """Seconds the current/last attach has been open (0 if it never opened)."""
        if self._attach_opened_at is None:
            return 0.0
        return time.monotonic() - self._attach_opened_at

    async def _attach_and_pump(self, target: registry.PanelTarget) -> None:
        uri = (
            f"ws://127.0.0.1:{native_chrome._cdp_port()}"
            f"/devtools/page/{target.target_id}"
        )
        self._clear_frame()
        self._attach_opened_at = None
        ws = await _open_upstream(uri)
        self._attach_opened_at = time.monotonic()
        self._upstream = ws
        self._attached_target_id = target.target_id
        broker = browser_control._brokers.get(self.session_id)
        self._frame_identity = (
            {"runtime_id": broker.runtime_id, "tab_id": target.tab_id}
            if broker and broker.runtime_id
            else {}
        )
        self._upstream_msg_id = 0
        self._pending_jumps.clear()
        self._pending_inspects.clear()
        self.page_url = target.url
        self.page_loading = False
        self._loading_frames.clear()
        change_task: Optional[asyncio.Task] = None
        recv_task: Optional[asyncio.Task] = None
        try:
            # Previews belong to connected viewers in Design mode, and each
            # puts its own back once the page is live. Anything already on the
            # page was left by a connection or an app run that has ended.
            await self._reset_design(ws, None)
            for viewer in self.viewers:
                viewer.design_active = False
            await self._send_upstream(ws, "Page.enable")
            # Chrome paints — and therefore screencasts — only the foreground
            # tab of a window. A panel attached to a backgrounded target reaches
            # "live" and then shows "waiting for the page to draw its first
            # frame" forever, which is exactly what the local lane reported
            # while the agent's own screenshots of the same page worked (those
            # force a paint). Activate the target before asking for frames.
            await self._send_upstream(ws, "Page.bringToFront")
            await self._send_upstream(ws, "Page.startScreencast", _SCREENCAST_PARAMS)

            # Ask for the current URL up front; the reply lands in the pump loop
            # below via the same pending-jumps channel (offset 0 = no jump).
            await self._send_upstream(ws, "Page.getNavigationHistory", response=True)
            self._pending_jumps[self._upstream_msg_id] = 0

            change_task = asyncio.create_task(registry.wait_for_change(self.session_id))
            recv_task = asyncio.create_task(ws.recv())
            live = False

            while True:
                done, _pending = await asyncio.wait(
                    {change_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if recv_task in done:
                    raw = recv_task.result()  # may raise on upstream close
                    recv_task = asyncio.create_task(ws.recv())
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    method = msg.get("method")
                    if "id" in msg and msg["id"] in self._pending_jumps:
                        is_attach_probe = self._pending_jumps.get(msg["id"]) == 0
                        await self._complete_history_jump(
                            msg["id"], msg.get("result", {})
                        )
                        if is_attach_probe and not live:
                            # The target answered the attach-time probe, so the
                            # attachment is healthy — declare live NOW instead
                            # of waiting for a first screencast frame. Chrome
                            # only paints frames for the FOREGROUND tab, so a
                            # backgrounded or static page may never produce one;
                            # gating "live" on frames kept the panel stuck on
                            # "connecting"/"Reconnecting…" through an entire
                            # streaming turn (the panel-flicker bug).
                            live = True
                            await self._broadcast_status(
                                "live", self.page_url or target.url
                            )
                        continue
                    if "id" in msg and msg["id"] in self._pending_inspects:
                        await self._complete_inspect(msg["id"], msg.get("result", {}))
                        continue
                    changed = False
                    if method in (
                        "Page.frameStartedLoading",
                        "Page.frameStoppedLoading",
                        "Page.frameDetached",
                        "Page.frameNavigated",
                        "Page.navigatedWithinDocument",
                    ):
                        changed = self._handle_page_event(msg)
                    elif method == "Page.frameResized":
                        # Chromium can stop producing screencast frames after a
                        # viewport change on a static page. Restart only the
                        # capture on the existing attachment so both viewers
                        # receive the new dimensions without changing control,
                        # navigating, or reconnecting the CDP socket.
                        await self._send_upstream(ws, "Page.stopScreencast")
                        await self._send_upstream(
                            ws, "Page.startScreencast", _SCREENCAST_PARAMS
                        )
                    elif method is None:
                        continue
                    if changed:
                        self._broadcast_page()
                        continue
                    if method == "Page.screencastFrame":
                        params = msg.get("params", {})
                        metadata = params.get("metadata", {})
                        # Enqueue to all viewers (non-blocking), THEN ack — acks
                        # must never wait on a slow viewer.
                        self._fan_out_frame(params.get("data", ""), metadata)
                        with suppress(Exception):
                            await self._send_upstream(
                                ws,
                                "Page.screencastFrameAck",
                                {"sessionId": params.get("sessionId")},
                            )
                        if not live:
                            live = True
                            await self._broadcast_status("live", target.url)
                if change_task in done:
                    change_task.result()
                    current = registry.get_target(self.session_id)
                    # A URL/tab metadata update on the SAME CDP target does not
                    # require an upstream teardown.  Keep the canvas-producing
                    # screencast alive, publish the fresh URL, and arm the next
                    # change waiter.  Only attachment identity changes reattach.
                    active_broker = browser_control._brokers.get(self.session_id)
                    active_runtime = active_broker.runtime_id if active_broker else None
                    if (
                        current is not None
                        and current.target_id == target.target_id
                        and active_runtime == self._frame_identity.get("runtime_id")
                        and current.tab_id == target.tab_id
                    ):
                        target = current
                        await self._broadcast_status(
                            "live" if live else "connecting", current.url
                        )
                        change_task = asyncio.create_task(
                            registry.wait_for_change(self.session_id)
                        )
                        continue
                    raise _RegistryChanged()
        finally:
            self._clear_frame()
            children = [task for task in (change_task, recv_task) if task is not None]
            for task in children:
                task.cancel()
            try:
                # Collect child cancellation without swallowing cancellation of
                # this hub task during a registry-change teardown. Otherwise
                # _run can reattach while stop() waits for it forever.
                await asyncio.gather(*children, return_exceptions=True)
            finally:
                # Leaving this page (another tab, the last viewer gone): it
                # goes back to its own styles first.
                if any(viewer.design_active for viewer in self.viewers):
                    for viewer in self.viewers:
                        viewer.design_active = False
                    with suppress(Exception):
                        await self._reset_design(ws, None)
                with suppress(Exception):
                    await self._send_upstream(ws, "Page.stopScreencast")
                with suppress(Exception):
                    await ws.close()
                self._upstream = None
                self._attached_target_id = None


# ---------------------------------------------------------------------------
# Module-level hub registry
# ---------------------------------------------------------------------------
_hubs: Dict[str, BrowserStreamHub] = {}
_hubs_lock = asyncio.Lock()


async def attach_viewer(
    session_id: str,
    websocket: WebSocket,
    *,
    redact_urls: bool = False,
) -> Tuple[BrowserStreamHub, _Viewer]:
    async with _hubs_lock:
        hub = _hubs.get(session_id)
        if hub is None:
            hub = BrowserStreamHub(session_id)
            _hubs[session_id] = hub
            hub.start()
        viewer = await hub.add_viewer(websocket, redact_urls=redact_urls)
        return hub, viewer


async def detach_viewer(session_id: str, viewer: _Viewer) -> None:
    async with _hubs_lock:
        hub = _hubs.get(session_id)
        if hub is None:
            return
        await hub.remove_viewer(viewer)
        if hub.viewer_count() == 0:
            await hub.stop()
            _hubs.pop(session_id, None)
