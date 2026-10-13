"""Element targeting through Windows UI Automation (UIA), for computer use.

``ui_tree`` lists a window's elements and ``click_element`` / ``set_value``
act on one by id. Everything here fails soft: no UIA, no ``comtypes``, a hung
app or a timeout becomes a coded ``ActionError`` that tells the model to fall
back to screenshot coordinates. Nothing raises into the session.

Threading. UIA calls go into the target app's process and can block for as
long as that app is hung, so every call runs on a worker thread (COM
initialised there as multithreaded, the apartment UIA clients should use)
under a time budget (``CALL_BUDGET_S``). A call that runs past it is
abandoned: the caller gets an error and the next call starts a fresh worker.
The tool thread polls the session's stop switch while it waits.

Nothing in this module imports ``comtypes`` or touches Win32 at import time;
``ComProvider`` does that on its worker thread, on Windows only. Tests inject a
fake provider through ``Backend(factory)``.
"""

from __future__ import annotations

import importlib.util
import logging
import queue
import sys
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional, Protocol

from app.mcp.computer_use.abort import Aborted
from app.mcp.computer_use.errors import ActionError

logger = logging.getLogger(__name__)

MAX_ELEMENTS = 200      # elements one ui_tree call returns
MAX_DEPTH = 8           # levels of listed-kind elements below the window
RAW_DEPTH_LIMIT = 40    # tree levels walked at most, layout wrappers included
MAX_CHILDREN = 50       # children read per parent, so one long list cannot fill the cap
TEXT_CHARS = 200        # names and values are cut to this length
CALL_BUDGET_S = 2.0     # hard budget per UIA call
WALK_BUDGET_S = 1.4     # a tree walk stops here and reports what it read
INIT_BUDGET_S = 10.0    # first use: import comtypes, load the UIA type library
_MAX_STUCK_WORKERS = 4  # abandoned workers still waiting on hung apps
_POLL_S = 0.03

FALLBACK = "Use screenshot coordinates instead."

# Routes an element action can take. "mouse" is a real click at the element's
# centre through the normal click path.
PATTERN_ROUTES = ("invoke", "toggle", "select", "expand", "collapse")
CLICK_ORDER = ("toggle", "select", "invoke", "expand", "collapse")
ROUTE_CHOICES = ("auto", *PATTERN_ROUTES, "mouse")

CONTROL_TYPES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar", 50015: "Slider",
    50016: "Spinner", 50017: "StatusBar", 50018: "Tab", 50019: "TabItem",
    50020: "Text", 50021: "ToolBar", 50022: "ToolTip", 50023: "Tree",
    50024: "TreeItem", 50025: "Custom", 50026: "Group", 50027: "Thumb",
    50028: "DataGrid", 50029: "DataItem", 50030: "Document", 50031: "SplitButton",
    50032: "Window", 50033: "Pane", 50034: "Header", 50035: "HeaderItem",
    50036: "Table", 50037: "TitleBar", 50038: "Separator", 50039: "SemanticZoom",
    50040: "AppBar",
}


@dataclass(frozen=True)
class Element:
    """One UIA element as plain data (no COM objects leave the worker)."""

    runtime_id: tuple[int, ...]
    control_type: str
    name: str
    value: Optional[str] = None      # None: no value, or a password field
    enabled: bool = True
    focusable: bool = False
    password: bool = False
    offscreen: bool = False
    rect: Optional[tuple[int, int, int, int]] = None  # physical left, top, right, bottom
    actions: tuple[str, ...] = ()    # invoke, toggle, select, expand, collapse, set_value
    toggle_state: Optional[str] = None  # "on", "off", "indeterminate"
    automation_id: str = ""
    depth: int = 1
    parent: Optional[int] = None     # index of the nearest listed ancestor in the tree

    @property
    def interactive(self) -> bool:
        return bool(self.actions) or self.focusable

    def center(self) -> Optional[tuple[int, int]]:
        if not self.rect:
            return None
        left, top, right, bottom = self.rect
        if right <= left or bottom <= top:
            return None
        return (left + right) // 2, (top + bottom) // 2


class Provider(Protocol):
    """What the backend needs from a UIA implementation. Every method runs on
    the backend's worker thread; handles never leave it."""

    def tree(self, hwnd: int, *, max_elements: int, max_depth: int, max_children: int,
             interactive_only: bool, deadline: float) -> tuple[list[Element], Optional[str]]:
        """Elements under the window (not the window itself), and why the
        list stopped early (None when it is complete)."""
        ...

    def find(self, hwnd: int, runtime_id: tuple[int, ...]) -> Optional[tuple[Element, Any]]:
        """The element with this runtime id inside the window, re-read now,
        with a handle for ``perform``/``read``; None when it is gone."""
        ...

    def perform(self, handle: Any, route: str, value: Optional[str] = None) -> None:
        """Run one pattern action (a PATTERN_ROUTES entry, or "set_value")."""
        ...

    def read(self, handle: Any) -> Element:
        """Re-read the element after an action."""
        ...

    def window_at(self, x: int, y: int) -> Optional[int]:
        """The top-level window under a physical desktop point."""
        ...

    def close(self) -> None:
        """Release COM state (called on the worker thread as it exits)."""
        ...


_LAYOUT_TYPES = frozenset({"Pane", "Group", "Custom"})


def is_layout(element: Element) -> bool:
    """An unnamed, inert container (web and Electron apps nest controls ten or
    more of these deep). Not listed and not counted toward MAX_DEPTH."""
    return (element.control_type in _LAYOUT_TYPES and not element.name
            and not element.interactive)


def clip(text: Any, limit: int = TEXT_CHARS) -> str:
    text = "" if text is None else str(text)
    text = " ".join(text.split()) if "\n" in text or "\r" in text else text
    return text if len(text) <= limit else text[: limit - 1] + "…"


def choose_route(element: Element, route: str = "auto") -> str:
    """The route ``click_element`` takes. "auto" acts like a single click:
    the element's Toggle, Select, Invoke or Expand/Collapse action, in that
    order (Select before Invoke, so a file in a list is selected, not opened),
    else a real click at its centre. "mouse" always clicks; a named route
    must be an action the element has."""
    if route == "mouse":
        return "mouse"
    if route == "auto":
        return next((r for r in CLICK_ORDER if r in element.actions), "mouse")
    if route in PATTERN_ROUTES and route in element.actions:
        return route
    raise ActionError(
        "UNSUPPORTED_CAPABILITY",
        f"This {element.control_type} has no {route} action (it has: "
        f"{', '.join(element.actions) or 'none'}). Use route 'auto' or 'mouse'.")


def same_target(stored: Element, current: Element) -> bool:
    return (stored.control_type == current.control_type
            and stored.name == current.name)


def available() -> tuple[bool, str]:
    """Whether UIA can work here, without importing anything heavy."""
    if sys.platform != "win32":
        return False, "Windows only"
    if importlib.util.find_spec("comtypes") is None:
        return False, "the comtypes package is not installed"
    return True, "ok"


# ---------------------------------------------------------------------------
# Worker thread and budgets
# ---------------------------------------------------------------------------

class _Worker:
    """One thread that owns a provider and runs calls in order."""

    def __init__(self, factory: Callable[[], Provider]):
        self.factory = factory
        self.tasks: "queue.SimpleQueue" = queue.SimpleQueue()
        self.ready = threading.Event()
        self.init_error: Optional[BaseException] = None
        self.thread = threading.Thread(target=self._loop, name="cu-uia", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        provider: Optional[Provider] = None
        try:
            provider = self.factory()
        except BaseException as exc:  # reported to every caller
            self.init_error = exc
        self.ready.set()
        try:
            while True:
                item = self.tasks.get()
                if item is None:
                    return
                fn, fut = item
                if not fut.set_running_or_notify_cancel():
                    continue
                if self.init_error is not None:
                    fut.set_exception(self.init_error)
                    continue
                try:
                    fut.set_result(fn(provider))
                except BaseException as exc:
                    fut.set_exception(exc)
        finally:
            if provider is not None:
                try:
                    provider.close()
                except Exception:
                    logger.debug("UIA provider close failed", exc_info=True)

    def retire(self) -> None:
        self.tasks.put(None)


class Backend:
    """Runs provider calls on a worker thread under a time budget."""

    def __init__(self, factory: Optional[Callable[[], Provider]] = None,
                 budget: float = CALL_BUDGET_S, init_budget: float = INIT_BUDGET_S):
        self.factory = factory or ComProvider
        self.budget = budget
        self.init_budget = init_budget
        self._lock = threading.Lock()
        self._worker: Optional[_Worker] = None
        self._stuck: list[_Worker] = []
        self.unavailable: Optional[str] = None  # last reason UIA could not start

    def _current(self) -> _Worker:
        with self._lock:
            self._stuck = [w for w in self._stuck if w.thread.is_alive()]
            if self._worker is None:
                if len(self._stuck) >= _MAX_STUCK_WORKERS:
                    raise ActionError(
                        "UNSUPPORTED_CAPABILITY",
                        "UI Automation is still waiting on apps that stopped responding. " + FALLBACK)
                self._worker = _Worker(self.factory)
            return self._worker

    def _abandon(self, worker: _Worker) -> None:
        with self._lock:
            if self._worker is worker:
                self._worker = None
            worker.retire()
            self._stuck.append(worker)

    def call(self, fn: Callable[[Provider], Any], check: Callable[[], None],
             on_timeout: Optional[Callable[[], ActionError]] = None,
             budget: Optional[float] = None) -> Any:
        """Run ``fn(provider)`` on the worker; poll ``check`` (the stop
        switch) while waiting. Past the budget the worker is abandoned and
        ``on_timeout()`` (or a default UNSUPPORTED_CAPABILITY) is raised."""
        worker = self._current()
        if not worker.ready.is_set():
            end = time.monotonic() + self.init_budget
            while not worker.ready.wait(_POLL_S):
                check()
                if time.monotonic() >= end:
                    self._abandon(worker)
                    raise ActionError("UNSUPPORTED_CAPABILITY",
                                      "UI Automation did not start in time. " + FALLBACK)
        if worker.init_error is not None:
            self._abandon(worker)
            raise self._init_failure(worker.init_error)
        fut: Future = Future()
        worker.tasks.put((fn, fut))
        end = time.monotonic() + (self.budget if budget is None else budget)
        while True:
            try:
                return fut.result(timeout=_POLL_S)
            except FutureTimeout:
                pass
            try:
                check()
            except BaseException:
                if not fut.done():
                    self._abandon(worker)
                raise
            if time.monotonic() >= end:
                if fut.done():
                    continue
                self._abandon(worker)
                logger.warning("computer-use UIA call ran past its %.1f s budget; worker abandoned",
                               self.budget if budget is None else budget)
                raise (on_timeout() if on_timeout else ActionError(
                    "UNSUPPORTED_CAPABILITY",
                    "The app did not answer UI Automation in time (it may be busy or not "
                    "responding). " + FALLBACK))

    def _init_failure(self, exc: BaseException) -> ActionError:
        if isinstance(exc, ActionError):
            self.unavailable = str(exc)
            return exc
        if isinstance(exc, ImportError):
            self.unavailable = "the comtypes package is not installed"
        else:
            self.unavailable = f"UI Automation could not start ({type(exc).__name__})"
        logger.info("computer-use UIA unavailable: %s", exc)
        return ActionError("UNSUPPORTED_CAPABILITY",
                           f"UI Automation is not available: {self.unavailable}. " + FALLBACK)

    def capability(self) -> dict:
        """Whether element tools can work, without starting UIA."""
        ok, reason = available() if self.factory is ComProvider else (True, "ok")
        if ok and self.unavailable:
            ok, reason = False, self.unavailable
        return {"available": ok, "reason": None if ok else reason}

    def shutdown(self) -> None:
        with self._lock:
            if self._worker is not None:
                self._worker.retire()
                self._worker = None


# ---------------------------------------------------------------------------
# The real provider: comtypes against UIAutomationCore (Windows only)
# ---------------------------------------------------------------------------

_COINIT_MULTITHREADED = 0
_TREE_SCOPE_DESCENDANTS = 4
_RPC_E_CHANGED_MODE = -2147417850
# UIA_* property ids.
_P_RUNTIME_ID, _P_RECT, _P_TYPE, _P_NAME = 30000, 30001, 30003, 30005
_P_FOCUSABLE, _P_ENABLED, _P_AUTOMATION_ID = 30009, 30010, 30011
_P_PASSWORD, _P_OFFSCREEN = 30019, 30022
_P_HAS_EXPAND, _P_HAS_INVOKE, _P_HAS_SELECT = 30028, 30031, 30036
_P_HAS_TOGGLE, _P_HAS_VALUE = 30041, 30043
_P_VALUE, _P_VALUE_READONLY = 30045, 30046
_P_EXPAND_STATE, _P_TOGGLE_STATE = 30070, 30086
_PROPS = (_P_RUNTIME_ID, _P_RECT, _P_TYPE, _P_NAME, _P_FOCUSABLE, _P_ENABLED,
          _P_AUTOMATION_ID, _P_PASSWORD, _P_OFFSCREEN, _P_HAS_EXPAND, _P_HAS_INVOKE,
          _P_HAS_SELECT, _P_HAS_TOGGLE, _P_HAS_VALUE, _P_VALUE, _P_VALUE_READONLY,
          _P_EXPAND_STATE, _P_TOGGLE_STATE)
# UIA_* pattern ids.
_PATTERNS = {"invoke": 10000, "set_value": 10002, "expand": 10005, "collapse": 10005,
             "select": 10010, "toggle": 10015}
_TOGGLE_STATES = {0: "off", 1: "on", 2: "indeterminate"}
_EXPAND_LEAF = 3


class ComProvider:
    """UIA through comtypes. Built on, and only used from, the worker thread."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise ActionError("UNSUPPORTED_CAPABILITY", "UI Automation is Windows only. " + FALLBACK)
        # comtypes initialises COM on the thread that first imports it, as
        # single-threaded unless told otherwise. That is this worker thread.
        if not hasattr(sys, "coinit_flags"):
            sys.coinit_flags = _COINIT_MULTITHREADED  # type: ignore[attr-defined]
        import comtypes
        import comtypes.client
        from app.mcp.computer_use import scaling
        self._uninit = False
        try:
            comtypes.CoInitializeEx(_COINIT_MULTITHREADED)
            self._uninit = True
        except OSError as exc:
            # Already initialised on this thread (by the import): fine.
            if getattr(exc, "winerror", None) not in (None, _RPC_E_CHANGED_MODE):
                raise
        # Bounding rectangles in physical pixels, like screenshots and input.
        scaling._ensure_dpi_awareness()
        self._comtypes = comtypes
        self.UIA = comtypes.client.GetModule("UIAutomationCore.dll")
        U = self.UIA
        self.auto = comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
        try:
            auto2 = self.auto.QueryInterface(U.IUIAutomation2)
            # Let UIA give up on a hung app on its own too, so an abandoned
            # worker does not wait for ever.
            auto2.ConnectionTimeout = 1000
            auto2.TransactionTimeout = int(CALL_BUDGET_S * 1000)
        except Exception:
            logger.debug("IUIAutomation2 timeouts not available", exc_info=True)
        cache = self.auto.CreateCacheRequest()
        for pid in _PROPS:
            cache.AddProperty(pid)
        self.cache = cache
        self.walker = self.auto.ControlViewWalker
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
        user32.GetAncestor.restype = wintypes.HWND
        self._user32, self._point = user32, wintypes.POINT

    # ----- reading -----------------------------------------------------------

    def _element(self, el, cached: bool, depth: int = 1, parent: Optional[int] = None) -> Element:
        get = el.GetCachedPropertyValue if cached else el.GetCurrentPropertyValue

        def prop(pid, default=None):
            try:
                value = get(pid)
            except Exception:
                return default
            return default if value is None else value

        rect = None
        raw = prop(_P_RECT)
        if raw and len(raw) == 4:
            left, top, width, height = (float(v) for v in raw)
            if width > 0 and height > 0:
                rect = (int(round(left)), int(round(top)),
                        int(round(left + width)), int(round(top + height)))
        password = bool(prop(_P_PASSWORD, False))
        actions = []
        if prop(_P_HAS_INVOKE, False):
            actions.append("invoke")
        toggle = None
        if prop(_P_HAS_TOGGLE, False):
            actions.append("toggle")
            toggle = _TOGGLE_STATES.get(prop(_P_TOGGLE_STATE))
        if prop(_P_HAS_SELECT, False):
            actions.append("select")
        if prop(_P_HAS_EXPAND, False):
            state = prop(_P_EXPAND_STATE)
            if state != _EXPAND_LEAF:
                actions.append("collapse" if state in (1, 2) else "expand")
        value = None
        if prop(_P_HAS_VALUE, False):
            if not prop(_P_VALUE_READONLY, False):
                actions.append("set_value")
            if not password:
                value = clip(prop(_P_VALUE, ""))
        runtime = prop(_P_RUNTIME_ID, ()) or ()
        ctype = prop(_P_TYPE, 0)
        return Element(
            runtime_id=tuple(int(v) for v in runtime),
            control_type=CONTROL_TYPES.get(ctype, f"Type{ctype}"),
            name=clip(prop(_P_NAME, "")), value=value,
            enabled=bool(prop(_P_ENABLED, True)), focusable=bool(prop(_P_FOCUSABLE, False)),
            password=password, offscreen=bool(prop(_P_OFFSCREEN, False)), rect=rect,
            actions=tuple(actions), toggle_state=toggle,
            automation_id=clip(prop(_P_AUTOMATION_ID, ""), 80), depth=depth, parent=parent)

    def _root(self, hwnd: int):
        return self.auto.ElementFromHandle(hwnd)

    def tree(self, hwnd, *, max_elements, max_depth, max_children, interactive_only, deadline):
        root = self.auto.ElementFromHandleBuildCache(hwnd, self.cache)
        out: list[Element] = []
        truncated: Optional[str] = None
        # Breadth first, so a long list deep in one pane cannot use the whole
        # cap before the rest of the window is seen. Entries carry the nearest
        # listed ancestor and the depth counted so far (layout wrappers are
        # neither listed nor counted; see is_layout).
        level = [(root, None, 0)]
        for _ in range(RAW_DEPTH_LIMIT):
            nxt = []
            for parent, parent_index, used in level:
                if used >= max_depth:
                    if truncated is None and self._has_child(parent):
                        truncated = "depth"
                    continue
                try:
                    child = self.walker.GetFirstChildElementBuildCache(parent, self.cache)
                except Exception:
                    continue
                count = 0
                # comtypes returns a NULL pointer, not None, at the end.
                while child:
                    if time.monotonic() >= deadline:
                        return out, "time"
                    if count >= max_children:
                        truncated = truncated or "children"
                        break
                    count += 1
                    try:
                        el = self._element(child, cached=True, depth=used + 1, parent=parent_index)
                    except Exception:
                        el = None
                    index, depth = parent_index, used
                    if el is not None and not is_layout(el):
                        depth = used + 1
                        if el.interactive or not interactive_only:
                            if len(out) >= max_elements:
                                return out, "count"
                            index = len(out)
                            out.append(el)
                    nxt.append((child, index, depth))
                    try:
                        child = self.walker.GetNextSiblingElementBuildCache(child, self.cache)
                    except Exception:
                        break
            if not nxt:
                return out, truncated
            level = nxt
        return out, truncated or "depth"

    def _has_child(self, el) -> bool:
        try:
            return bool(self.walker.GetFirstChildElement(el))
        except Exception:
            return False

    def find(self, hwnd, runtime_id):
        import array
        from comtypes.automation import VARIANT
        root = self._root(hwnd)
        cond = self.auto.CreatePropertyCondition(_P_RUNTIME_ID, VARIANT(array.array("l", runtime_id)))
        el = root.FindFirstBuildCache(_TREE_SCOPE_DESCENDANTS, cond, self.cache)
        if not el:
            return None
        return self._element(el, cached=True), el

    def perform(self, handle, route, value=None):
        U = self.UIA
        iface = {"invoke": U.IUIAutomationInvokePattern, "toggle": U.IUIAutomationTogglePattern,
                 "select": U.IUIAutomationSelectionItemPattern,
                 "expand": U.IUIAutomationExpandCollapsePattern,
                 "collapse": U.IUIAutomationExpandCollapsePattern,
                 "set_value": U.IUIAutomationValuePattern}[route]
        unknown = handle.GetCurrentPattern(_PATTERNS[route])
        if not unknown:
            raise ActionError("UNSUPPORTED_CAPABILITY",
                              f"The element no longer supports {route}. " + FALLBACK)
        pattern = unknown.QueryInterface(iface)
        if route == "invoke":
            pattern.Invoke()
        elif route == "toggle":
            pattern.Toggle()
        elif route == "select":
            pattern.Select()
        elif route == "expand":
            pattern.Expand()
        elif route == "collapse":
            pattern.Collapse()
        else:
            pattern.SetValue(str(value))

    def read(self, handle):
        return self._element(handle, cached=False)

    def window_at(self, x, y):
        hwnd = self._user32.WindowFromPoint(self._point(int(x), int(y)))
        if not hwnd:
            return None
        root = self._user32.GetAncestor(hwnd, 2)  # GA_ROOT
        return int(root or hwnd)

    def close(self):
        comtypes, uninit = self._comtypes, self._uninit
        self.__dict__.clear()  # release the COM pointers before uninitialising
        if uninit:
            comtypes.CoUninitialize()


# ---------------------------------------------------------------------------
# The operations the tools run (generic over the provider)
# ---------------------------------------------------------------------------

def is_gone(exc: BaseException) -> bool:
    """A COM error meaning the element no longer exists."""
    code = getattr(exc, "hresult", None)
    if code is None and getattr(exc, "args", None):
        code = exc.args[0] if isinstance(exc.args[0], int) else None
    # UIA_E_ELEMENTNOTAVAILABLE, UIA_E_ELEMENTNOTENABLED, RPC_E_DISCONNECTED,
    # RPC_S_SERVER_UNAVAILABLE (as HRESULT), E_HANDLE.
    return code in (-2147220991, -2147220992, -2147417848, -2147023174, -2147024890)


def read_tree(backend: Backend, hwnd: int, check: Callable[[], None], *,
              interactive_only: bool = False) -> tuple[list[Element], Optional[str]]:
    def run(provider: Provider):
        deadline = time.monotonic() + WALK_BUDGET_S
        return provider.tree(hwnd, max_elements=MAX_ELEMENTS, max_depth=MAX_DEPTH,
                             max_children=MAX_CHILDREN, interactive_only=interactive_only,
                             deadline=deadline)
    try:
        return backend.call(run, check)
    except (ActionError, Aborted):
        raise
    except Exception as exc:
        logger.info("computer-use ui_tree failed: %r", exc)
        raise ActionError("UNSUPPORTED_CAPABILITY",
                          "UI Automation could not read this window. " + FALLBACK)


@dataclass
class ActResult:
    route: str
    element: Element          # as found before the action
    after: Optional[Element]  # re-read after a pattern action


def act(backend: Backend, hwnd: int, stored: Element, *, route_for: Callable[[Element], str],
        value: Optional[str], check: Callable[[], None],
        on_dispatch: Callable[[], None]) -> ActResult:
    """Re-resolve ``stored`` in its window, verify it is the same target, then
    run its pattern action. Returns route "mouse" without acting when the
    caller should click its centre instead."""
    state = {"dispatched": False}

    def run(provider: Provider) -> ActResult:
        found = provider.find(hwnd, stored.runtime_id)
        if found is None:
            raise ActionError("TARGET_CHANGED", "The element is gone (the window changed or "
                              "closed). Take a screenshot and call ui_tree again.")
        current, handle = found
        if not same_target(stored, current):
            raise ActionError(
                "TARGET_CHANGED",
                f"The element changed: it was {stored.control_type} {stored.name!r}, now "
                f"{current.control_type} {current.name!r}. Nothing was done. Take a "
                "screenshot and call ui_tree again.")
        current = replace(current, depth=stored.depth, parent=stored.parent)
        route = route_for(current)
        if route == "mouse":
            return ActResult("mouse", current, None)
        if not current.enabled:
            raise ActionError("ACTION_FAILED", "The element is disabled. Nothing was done.")
        check()  # last stop check before anything reaches the app
        state["dispatched"] = True
        on_dispatch()
        provider.perform(handle, route, value)
        try:
            after = provider.read(handle)
        except Exception:
            after = None
        return ActResult(route, current, after)

    def timed_out() -> ActionError:
        if state["dispatched"]:
            return ActionError(
                "EXECUTION_OUTCOME_UNKNOWN",
                "The app did not finish the action within 2 s; it may have opened a dialog or "
                "be busy. Take a screenshot before retrying.", outcome="unknown")
        return ActionError("UNSUPPORTED_CAPABILITY",
                           "The app did not answer UI Automation in time. " + FALLBACK)

    try:
        return backend.call(run, check, on_timeout=timed_out)
    except (ActionError, Aborted):
        raise
    except Exception as exc:
        if is_gone(exc):
            raise ActionError("TARGET_CHANGED", "The element is gone. Take a screenshot and "
                              "call ui_tree again.",
                              outcome="unknown" if state["dispatched"] else "rejected")
        logger.info("computer-use element action failed: %r", exc)
        if state["dispatched"]:
            raise ActionError("EXECUTION_OUTCOME_UNKNOWN", "The app reported an error during the "
                              "action. Take a screenshot before retrying.", outcome="unknown")
        raise ActionError("ACTION_FAILED", "UI Automation could not act on the element. " + FALLBACK)


def window_at(backend: Backend, x: int, y: int, check: Callable[[], None]) -> Optional[int]:
    try:
        return backend.call(lambda provider: provider.window_at(x, y), check)
    except (ActionError, Aborted):
        raise
    except Exception:
        logger.debug("computer-use window_at failed", exc_info=True)
        return None
