"""
MCP Tools for web browser capabilities.

Provides tools that Claude can call to control a browser, including:
- Navigation (URLs, back, forward)
- Page interaction (click, type, fill, select)
- Content extraction (readable text, accessibility tree, interactive elements)
- Screenshots (explicit opt-in for visual verification)
- Tab management
- JavaScript evaluation

Tools use Chrome DevTools Protocol (CDP) via CDPClient for browser automation.
This replaces the previous Playwright-based implementation.

Each tool uses the ChromeSessionManager for per-session Chrome instances.
"""

import asyncio
import contextvars
import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.mcp.chrome_session import chrome_session_manager, ChromeSession

logger = logging.getLogger(__name__)

# Session ID is injected by the query engine when calling tools.
# Uses ContextVar so concurrent sessions each get their own value.
_current_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    '_current_session_id', default=None
)


def set_session_id(session_id: str):
    """Set the current session ID for browser tool calls."""
    _current_session_id.set(session_id)


def get_session_id() -> str:
    """Get current session ID, raising if not set."""
    sid = _current_session_id.get()
    if not sid:
        raise ValueError("No browser session ID set. Browser tools require an active chat session.")
    return sid


# ============================================================================
# Helper Functions
# ============================================================================

def _text_result(data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a text-only MCP result."""
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(data, indent=2),
        }]
    }


def _text_and_image_result(data: Dict[str, Any], screenshot_b64: str) -> Dict[str, Any]:
    """Create an MCP result with text and screenshot image."""
    content = [
        {
            "type": "text",
            "text": json.dumps(data, indent=2),
        },
    ]
    if screenshot_b64:
        content.append({
            "type": "image",
            "data": screenshot_b64,
            "mimeType": "image/jpeg",
        })
    return {"content": content}


def _error_result(error: str) -> Dict[str, Any]:
    """Create an error MCP result."""
    return {
        "content": [{
            "type": "text",
            "text": json.dumps({"success": False, "error": error}, indent=2),
        }]
    }


def _truncate_text(text: str, max_chars: int = 4000) -> str:
    """Truncate text at paragraph boundaries to stay within token budget."""
    if len(text) <= max_chars:
        return text

    truncated = text[:max_chars]
    last_double_newline = truncated.rfind("\n\n")
    if last_double_newline > max_chars * 0.5:
        truncated = truncated[:last_double_newline]
    else:
        last_period = truncated.rfind(". ")
        if last_period > max_chars * 0.5:
            truncated = truncated[:last_period + 1]

    return truncated + f"\n\n[... truncated, {len(text) - len(truncated)} chars remaining]"


def _clean_text(text: str) -> str:
    """Clean extracted text: collapse whitespace, remove empty lines."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(lines).strip()


# ============================================================================
# Accessibility Tree Formatting
# ============================================================================

def _format_a11y_node(node: dict, depth: int = 0, max_depth: int = 6) -> str:
    """Format an accessibility tree node as indented text for AI consumption."""
    if depth > max_depth:
        return ""

    indent = "  " * depth
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")

    parts = [f"{indent}{role}"]
    if name:
        parts.append(f'"{name}"')
    if value:
        parts.append(f'value="{value}"')

    for prop in ("checked", "disabled", "expanded", "selected", "required", "pressed"):
        val = node.get(prop)
        if val is not None and val is not False:
            parts.append(f"{prop}={val}")

    result = " ".join(parts)

    children = node.get("children", [])
    child_lines = []
    for child in children:
        child_str = _format_a11y_node(child, depth + 1, max_depth)
        if child_str:
            child_lines.append(child_str)

    if child_lines:
        result += "\n" + "\n".join(child_lines)

    return result


def _a11y_to_text(snapshot: Optional[dict], max_chars: int = 3000) -> str:
    """Convert accessibility snapshot to concise text representation."""
    if not snapshot:
        return "[Accessibility tree unavailable]"
    text = _format_a11y_node(snapshot)
    return _truncate_text(text, max_chars)


async def _get_session() -> ChromeSession:
    """Get or create browser session for current chat."""
    session_id = get_session_id()
    return await chrome_session_manager.get_or_create_session(session_id)


# ============================================================================
# JavaScript helpers for element interaction via CDP
# ============================================================================

# JS to find interactive elements and return their attributes + bounding rects
_GET_INTERACTIVE_ELEMENTS_JS = """
(() => {
    const selector = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"]';
    const nodes = document.querySelectorAll(selector);
    const results = [];
    const limit = Math.min(nodes.length, 50);
    for (let i = 0; i < limit; i++) {
        const el = nodes[i];
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;

        const tag = el.tagName.toLowerCase();
        const noText = ['input', 'select', 'textarea'];
        const textContent = noText.includes(tag) ? '' : (el.innerText || '').trim().slice(0, 100);
        const ariaLabel = el.getAttribute('aria-label') || '';
        const placeholder = el.placeholder || '';
        const elName = el.name || '';

        results.push({
            tag: tag,
            type: el.type || '',
            textContent: textContent,
            placeholder: placeholder,
            href: el.href || '',
            role: el.getAttribute('role') || '',
            ariaLabel: ariaLabel,
            name: elName,
            value: el.value || '',
            optionCount: tag === 'select' ? el.options.length : 0,
            label: ariaLabel || textContent || placeholder || elName || '',
            // Bounding rect for click targeting
            x: Math.round(rect.x + rect.width / 2),
            y: Math.round(rect.y + rect.height / 2),
        });
    }
    return results;
})()
"""

# JS to click an element by finding it via the same selector and index
_CLICK_ELEMENT_BY_INDEX_JS = """
((index) => {
    const selector = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"]';
    const nodes = document.querySelectorAll(selector);
    const visible = [];
    for (let i = 0; i < nodes.length && visible.length < 50; i++) {
        const el = nodes[i];
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        visible.push(el);
    }
    if (index < 0 || index >= visible.length) return null;
    const el = visible[index];
    el.click();
    const rect = el.getBoundingClientRect();
    return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
})(%d)
"""

# JS to click by visible text
_CLICK_BY_TEXT_JS = """
((searchText) => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
        const node = walker.currentNode;
        if (node.textContent.trim().toLowerCase().includes(searchText.toLowerCase())) {
            const el = node.parentElement;
            if (el) {
                el.click();
                const rect = el.getBoundingClientRect();
                return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
            }
        }
    }
    // Fallback: try querySelector with text matching
    const all = document.querySelectorAll('a, button, [role="button"], [role="link"]');
    for (const el of all) {
        if (el.textContent.trim().toLowerCase().includes(searchText.toLowerCase())) {
            el.click();
            const rect = el.getBoundingClientRect();
            return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
        }
    }
    return null;
})(%s)
"""

# JS to click by CSS selector
_CLICK_BY_SELECTOR_JS = """
((selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    el.click();
    const rect = el.getBoundingClientRect();
    return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
})(%s)
"""

# JS to fill a form field by various label strategies
_FILL_BY_LABEL_JS = """
((label, value) => {
    // Strategy 1: label[for] association
    const labels = document.querySelectorAll('label');
    for (const lbl of labels) {
        if (lbl.textContent.trim().toLowerCase().includes(label.toLowerCase())) {
            const forId = lbl.getAttribute('for');
            let input = forId ? document.getElementById(forId) : lbl.querySelector('input, textarea, select');
            if (input) {
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                )?.set || Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                )?.set;
                if (nativeInputValueSetter) {
                    nativeInputValueSetter.call(input, value);
                } else {
                    input.value = value;
                }
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
        }
    }
    // Strategy 2: placeholder
    let el = document.querySelector(`input[placeholder*="${label}" i], textarea[placeholder*="${label}" i]`);
    if (el) { el.value = value; el.dispatchEvent(new Event('input', {bubbles: true})); return true; }
    // Strategy 3: aria-label
    el = document.querySelector(`[aria-label*="${label}" i]`);
    if (el) { el.value = value; el.dispatchEvent(new Event('input', {bubbles: true})); return true; }
    // Strategy 4: name attribute
    el = document.querySelector(`[name="${label}"]`);
    if (el) { el.value = value; el.dispatchEvent(new Event('input', {bubbles: true})); return true; }
    return false;
})(%s, %s)
"""

# JS to select an option from a dropdown
_SELECT_OPTION_JS = """
((selector, value) => {
    const el = document.querySelector(selector);
    if (!el || el.tagName.toLowerCase() !== 'select') return false;
    // Try by value first
    for (const opt of el.options) {
        if (opt.value === value) {
            el.value = opt.value;
            el.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }
    }
    // Try by label text
    for (const opt of el.options) {
        if (opt.text.trim().toLowerCase() === value.toLowerCase()) {
            el.value = opt.value;
            el.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }
    }
    return false;
})(%s, %s)
"""


def _format_element(index: int, el: Dict[str, Any]) -> str:
    """Format an element for display to the AI."""
    tag = el["tag"]
    label = el["label"]

    if tag == "a":
        href = el["href"]
        return f'[{index}] link "{label}" → {href}'
    elif tag == "button" or el["role"] == "button":
        return f'[{index}] button "{label}"'
    elif tag == "input":
        input_type = el["type"] or "text"
        parts = [f"[{index}] input[type={input_type}]"]
        if label:
            parts.append(f'"{label}"')
        if el["placeholder"]:
            parts.append(f'placeholder="{el["placeholder"]}"')
        if el["value"]:
            parts.append(f'value="{el["value"]}"')
        return " ".join(parts)
    elif tag == "select":
        return f'[{index}] select "{label}" ({el["optionCount"]} options)'
    elif tag == "textarea":
        parts = [f"[{index}] textarea"]
        if label:
            parts.append(f'"{label}"')
        if el["placeholder"]:
            parts.append(f'placeholder="{el["placeholder"]}"')
        return " ".join(parts)
    else:
        role = el["role"]
        return f'[{index}] {tag}[role={role}] "{label}"'


# ============================================================================
# Browser Tool Implementations
# ============================================================================

async def browser_navigate(url: str) -> Dict[str, Any]:
    """Navigate to a URL and return page info with accessibility tree."""
    try:
        session = await _get_session()

        if not url.startswith(("http://", "https://", "file://", "data:")):
            url = "https://" + url

        await session.navigate(url)
        await asyncio.sleep(1.0)

        title = await session.get_title()
        current_url = await session.get_url()

        # Get brief content summary
        try:
            body_text = await session.evaluate("document.body ? document.body.innerText : ''")
            summary = _truncate_text(_clean_text(body_text or ""), max_chars=500)
        except Exception:
            summary = ""

        a11y = await session.get_accessibility_tree()
        a11y_text = _a11y_to_text(a11y)

        return _text_result({
            "success": True,
            "url": current_url,
            "title": title,
            "summary": summary,
            "accessibility_tree": a11y_text,
        })
    except Exception as e:
        return _error_result(f"Navigation failed: {str(e)}")


async def browser_screenshot(full_page: bool = False) -> Dict[str, Any]:
    """Take a screenshot of the current page (explicit visual verification)."""
    try:
        session = await _get_session()
        title = await session.get_title()
        url = await session.get_url()
        screenshot = await session.take_screenshot(full_page=full_page)

        return _text_and_image_result({
            "success": True,
            "url": url,
            "title": title,
        }, screenshot)
    except Exception as e:
        return _error_result(f"Screenshot failed: {str(e)}")


async def browser_click(
    selector: Optional[str] = None,
    text: Optional[str] = None,
    element_index: Optional[int] = None,
    x: Optional[int] = None,
    y: Optional[int] = None,
) -> Dict[str, Any]:
    """Click an element by selector, visible text, element index, or coordinates."""
    try:
        session = await _get_session()

        if element_index is not None:
            # Click by element index (1-based)
            result = await session.evaluate(_CLICK_ELEMENT_BY_INDEX_JS % (element_index - 1))
            if result is None:
                return _error_result(f"Element index {element_index} not found or out of range")
            clicked = f"element [{element_index}]"

        elif text:
            result = await session.evaluate(_CLICK_BY_TEXT_JS % json.dumps(text))
            if result is None:
                return _error_result(f'No element found with text "{text}"')
            clicked = f'text "{text}"'

        elif selector:
            result = await session.evaluate(_CLICK_BY_SELECTOR_JS % json.dumps(selector))
            if result is None:
                return _error_result(f'No element found for selector "{selector}"')
            clicked = f'selector "{selector}"'

        elif x is not None and y is not None:
            await session.click_at(x, y)
            clicked = f"coordinates ({x}, {y})"

        else:
            return _error_result("Provide selector, text, element_index, or x/y coordinates")

        await asyncio.sleep(0.5)

        title = await session.get_title()
        url = await session.get_url()
        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "clicked": clicked,
            "url": url,
            "title": title,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Click failed: {str(e)}")


async def browser_type(
    text: str,
    selector: Optional[str] = None,
    press_enter: bool = False,
) -> Dict[str, Any]:
    """Type text into the focused element or a specified selector."""
    try:
        session = await _get_session()

        if selector:
            await session.type_text(text, selector=selector)
            target = f'selector "{selector}"'
        else:
            await session.type_text(text)
            target = "focused element"

        if press_enter:
            await session.cdp_key_event("keyDown", key="Enter", code="Enter")
            await session.cdp_key_event("keyUp", key="Enter", code="Enter")

        await asyncio.sleep(0.3)
        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "typed": text,
            "target": target,
            "pressed_enter": press_enter,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Type failed: {str(e)}")


async def browser_scroll(
    direction: str = "down",
    amount: int = 3,
) -> Dict[str, Any]:
    """Scroll the page up or down."""
    try:
        session = await _get_session()

        pixels = amount * 300
        if direction == "up":
            pixels = -pixels

        await session.evaluate(f"window.scrollBy(0, {pixels})")
        await asyncio.sleep(0.3)

        scroll_info = await session.evaluate("""
            (() => ({
                scrollY: window.scrollY,
                scrollHeight: document.documentElement.scrollHeight,
                clientHeight: document.documentElement.clientHeight,
            }))()
        """)

        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "direction": direction,
            "scroll_y": scroll_info["scrollY"] if scroll_info else 0,
            "page_height": scroll_info["scrollHeight"] if scroll_info else 0,
            "viewport_height": scroll_info["clientHeight"] if scroll_info else 0,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Scroll failed: {str(e)}")


async def browser_get_content(selector: Optional[str] = None) -> Dict[str, Any]:
    """Get readable text content from the page."""
    try:
        session = await _get_session()

        if selector:
            raw_text = await session.evaluate(
                f"(() => {{ const el = document.querySelector({json.dumps(selector)}); return el ? el.innerText : ''; }})()"
            )
        else:
            raw_text = await session.evaluate("document.body ? document.body.innerText : ''")

        cleaned = _clean_text(raw_text or "")
        content = _truncate_text(cleaned, max_chars=4000)

        title = await session.get_title()
        url = await session.get_url()

        return _text_result({
            "success": True,
            "url": url,
            "title": title,
            "content": content,
            "total_length": len(cleaned),
            "truncated": len(cleaned) > 4000,
        })
    except Exception as e:
        return _error_result(f"Content extraction failed: {str(e)}")


async def browser_get_elements(selector: Optional[str] = None) -> Dict[str, Any]:
    """Get interactive elements on the page as a numbered list."""
    try:
        session = await _get_session()
        elements = await session.evaluate(_GET_INTERACTIVE_ELEMENTS_JS)

        if not elements:
            elements = []

        formatted = []
        for i, el in enumerate(elements, 1):
            formatted.append(_format_element(i, el))

        title = await session.get_title()
        url = await session.get_url()

        return _text_result({
            "success": True,
            "url": url,
            "title": title,
            "element_count": len(elements),
            "elements": "\n".join(formatted),
            "hint": "Use browser_click with element_index=N to click an element",
        })
    except Exception as e:
        return _error_result(f"Element extraction failed: {str(e)}")


async def browser_back() -> Dict[str, Any]:
    """Navigate back in browser history."""
    try:
        session = await _get_session()
        await session.go_back()
        await asyncio.sleep(0.5)

        title = await session.get_title()
        url = await session.get_url()
        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "url": url,
            "title": title,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Back navigation failed: {str(e)}")


async def browser_forward() -> Dict[str, Any]:
    """Navigate forward in browser history."""
    try:
        session = await _get_session()
        await session.go_forward()
        await asyncio.sleep(0.5)

        title = await session.get_title()
        url = await session.get_url()
        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "url": url,
            "title": title,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Forward navigation failed: {str(e)}")


async def browser_evaluate(script: str) -> Dict[str, Any]:
    """Execute JavaScript in the browser and return the result."""
    try:
        session = await _get_session()
        result = await session.evaluate(script)

        try:
            result_str = json.dumps(result, indent=2, default=str)
        except (TypeError, ValueError):
            result_str = str(result)

        url = await session.get_url()

        return _text_result({
            "success": True,
            "result": result_str,
            "url": url,
        })
    except Exception as e:
        return _error_result(f"JavaScript evaluation failed: {str(e)}")


async def browser_fill(label: str, value: str) -> Dict[str, Any]:
    """Fill a form field identified by its label text."""
    try:
        session = await _get_session()

        filled = await session.evaluate(
            _FILL_BY_LABEL_JS % (json.dumps(label), json.dumps(value))
        )

        if not filled:
            return _error_result(
                f'Could not find form field with label "{label}". '
                'Try browser_get_elements to see available fields.'
            )

        await asyncio.sleep(0.3)
        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "label": label,
            "value": value,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Fill failed: {str(e)}")


async def browser_select(selector: str, value: str) -> Dict[str, Any]:
    """Select a dropdown option by value or label."""
    try:
        session = await _get_session()

        selected = await session.evaluate(
            _SELECT_OPTION_JS % (json.dumps(selector), json.dumps(value))
        )

        if not selected:
            return _error_result(f'Could not select "{value}" in "{selector}"')

        await asyncio.sleep(0.3)
        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "selector": selector,
            "value": value,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Select failed: {str(e)}")


async def browser_wait(
    selector: Optional[str] = None,
    state: str = "visible",
    timeout: int = 10000,
) -> Dict[str, Any]:
    """Wait for a condition (element visible, hidden, network idle)."""
    try:
        session = await _get_session()

        if selector:
            # Poll for element visibility
            deadline = asyncio.get_event_loop().time() + (timeout / 1000.0)
            found = False

            while asyncio.get_event_loop().time() < deadline:
                if state == "visible":
                    result = await session.evaluate(f"""
                        (() => {{
                            const el = document.querySelector({json.dumps(selector)});
                            if (!el) return false;
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0 &&
                                   style.display !== 'none' && style.visibility !== 'hidden';
                        }})()
                    """)
                elif state == "hidden":
                    result = await session.evaluate(f"""
                        (() => {{
                            const el = document.querySelector({json.dumps(selector)});
                            if (!el) return true;
                            const style = window.getComputedStyle(el);
                            return style.display === 'none' || style.visibility === 'hidden';
                        }})()
                    """)
                elif state == "attached":
                    result = await session.evaluate(
                        f"document.querySelector({json.dumps(selector)}) !== null"
                    )
                elif state == "detached":
                    result = await session.evaluate(
                        f"document.querySelector({json.dumps(selector)}) === null"
                    )
                else:
                    return _error_result(f"Unknown state: {state}")

                if result:
                    found = True
                    break
                await asyncio.sleep(0.25)

            if not found:
                return _error_result(
                    f'Timeout waiting for "{selector}" to be {state} '
                    f'after {timeout}ms'
                )
            waited_for = f'selector "{selector}" to be {state}'
        else:
            # Wait for network idle (approximate)
            await asyncio.sleep(min(timeout / 1000.0, 5.0))
            waited_for = "network idle (approximate)"

        a11y = await session.get_accessibility_tree()
        url = await session.get_url()

        return _text_result({
            "success": True,
            "waited_for": waited_for,
            "url": url,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Wait failed: {str(e)}")


async def browser_tab_new(url: Optional[str] = None) -> Dict[str, Any]:
    """Open a new browser tab."""
    try:
        session = await _get_session()
        target_url = url or "about:blank"
        if url and not url.startswith(("http://", "https://", "about:", "data:")):
            target_url = "https://" + url

        target_id = await session.new_tab(target_url)
        if not target_id:
            return _error_result("Failed to create new tab")

        await asyncio.sleep(1.0)

        title = await session.get_title()
        current_url = await session.get_url()
        tabs = await session.get_tabs()
        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "tab_count": len(tabs),
            "url": current_url,
            "title": title,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Tab creation failed: {str(e)}")


async def browser_tab_list() -> Dict[str, Any]:
    """List all open browser tabs."""
    try:
        session = await _get_session()
        pages = await session.get_tabs()

        tabs = []
        for i, page in enumerate(pages):
            tabs.append({
                "index": i + 1,
                "url": page.get("url", ""),
                "title": page.get("title", ""),
                "active": page.get("id") == session._active_target_id,
            })

        return _text_result({
            "success": True,
            "tab_count": len(tabs),
            "tabs": tabs,
        })
    except Exception as e:
        return _error_result(f"Tab listing failed: {str(e)}")


async def browser_tab_switch(index: int) -> Dict[str, Any]:
    """Switch to a tab by index (1-based)."""
    try:
        session = await _get_session()
        pages = await session.get_tabs()

        if index < 1 or index > len(pages):
            return _error_result(f"Tab index {index} out of range (1-{len(pages)})")

        target = pages[index - 1]
        target_id = target.get("id")
        if target_id:
            await session.switch_to_target(target_id)

        await asyncio.sleep(0.5)

        title = await session.get_title()
        url = await session.get_url()
        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "active_tab": index,
            "url": url,
            "title": title,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Tab switch failed: {str(e)}")


async def browser_close() -> Dict[str, Any]:
    """Close the browser session."""
    try:
        session_id = get_session_id()
        await chrome_session_manager.close_session(session_id)

        return _text_result({
            "success": True,
            "message": "Browser session closed",
        })
    except Exception as e:
        return _error_result(f"Close failed: {str(e)}")


async def browser_observe() -> Dict[str, Any]:
    """Observe the current page state without any side effects."""
    try:
        session = await _get_session()
        title = await session.get_title()
        url = await session.get_url()
        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "url": url,
            "title": title,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Observe failed: {str(e)}")


# ============================================================================
# Tool Registry
# ============================================================================

BROWSER_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "browser_navigate",
        "description": "Navigate to a URL in the browser. Returns page title, status, content summary, and accessibility tree.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to navigate to (e.g., 'https://example.com' or 'example.com')",
                },
            },
            "required": ["url"],
        },
        "handler": browser_navigate,
        "security_level": "safe",
    },
    {
        "name": "browser_screenshot",
        "description": "Take a screenshot for visual verification. Most tools return an accessibility tree instead. Use this only when you need to see the visual layout.",
        "input_schema": {
            "type": "object",
            "properties": {
                "full_page": {
                    "type": "boolean",
                    "description": "Capture the full scrollable page (default: false, captures viewport only)",
                    "default": False,
                },
            },
        },
        "handler": browser_screenshot,
        "security_level": "safe",
    },
    {
        "name": "browser_observe",
        "description": "Observe the current page state: URL, title, and accessibility tree. Lightweight, no side effects.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": browser_observe,
        "security_level": "safe",
    },
    {
        "name": "browser_click",
        "description": "Click an element on the page. Use element_index (from browser_get_elements), visible text, CSS selector, or x/y coordinates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_index": {"type": "integer", "description": "Element index from browser_get_elements (1-based)"},
                "text": {"type": "string", "description": "Visible text of the element to click"},
                "selector": {"type": "string", "description": "CSS selector of the element to click"},
                "x": {"type": "integer", "description": "X coordinate to click"},
                "y": {"type": "integer", "description": "Y coordinate to click"},
            },
        },
        "handler": browser_click,
        "security_level": "confirm",
    },
    {
        "name": "browser_type",
        "description": "Type text into the focused element or a specified selector. Optionally press Enter after typing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type"},
                "selector": {"type": "string", "description": "Optional CSS selector to type into"},
                "press_enter": {"type": "boolean", "description": "Press Enter after typing (default: false)", "default": False},
            },
            "required": ["text"],
        },
        "handler": browser_type,
        "security_level": "confirm",
    },
    {
        "name": "browser_scroll",
        "description": "Scroll the page up or down.",
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "description": "Scroll direction", "enum": ["up", "down"], "default": "down"},
                "amount": {"type": "integer", "description": "Number of scroll units (1 unit ≈ 300px, default: 3)", "default": 3},
            },
        },
        "handler": browser_scroll,
        "security_level": "safe",
    },
    {
        "name": "browser_get_content",
        "description": "Get the readable text content of the page. Returns clean text (not raw HTML), truncated to ~4000 chars.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Optional CSS selector to extract content from"},
            },
        },
        "handler": browser_get_content,
        "security_level": "safe",
    },
    {
        "name": "browser_get_elements",
        "description": "Get a numbered list of interactive elements on the page. Use the returned index numbers with browser_click(element_index=N).",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Optional CSS selector to scope the search"},
            },
        },
        "handler": browser_get_elements,
        "security_level": "safe",
    },
    {
        "name": "browser_back",
        "description": "Navigate back in browser history.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": browser_back,
        "security_level": "safe",
    },
    {
        "name": "browser_forward",
        "description": "Navigate forward in browser history.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": browser_forward,
        "security_level": "safe",
    },
    {
        "name": "browser_evaluate",
        "description": "Execute JavaScript code in the browser and return the result.",
        "input_schema": {
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "JavaScript code to execute"},
            },
            "required": ["script"],
        },
        "handler": browser_evaluate,
        "security_level": "sensitive",
    },
    {
        "name": "browser_fill",
        "description": "Fill a form field identified by its label, placeholder, aria-label, or name attribute.",
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Label text, placeholder, aria-label, or name of the form field"},
                "value": {"type": "string", "description": "Value to fill into the field"},
            },
            "required": ["label", "value"],
        },
        "handler": browser_fill,
        "security_level": "confirm",
    },
    {
        "name": "browser_select",
        "description": "Select an option from a dropdown/select element.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of the select element"},
                "value": {"type": "string", "description": "Option value or visible text to select"},
            },
            "required": ["selector", "value"],
        },
        "handler": browser_select,
        "security_level": "confirm",
    },
    {
        "name": "browser_wait",
        "description": "Wait for a condition — an element to appear/disappear, or network to become idle.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector to wait for"},
                "state": {"type": "string", "description": "State to wait for", "enum": ["visible", "hidden", "attached", "detached"], "default": "visible"},
                "timeout": {"type": "integer", "description": "Maximum wait time in milliseconds (default: 10000)", "default": 10000},
            },
        },
        "handler": browser_wait,
        "security_level": "safe",
    },
    {
        "name": "browser_tab_new",
        "description": "Open a new browser tab, optionally navigating to a URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Optional URL to open in the new tab"},
            },
        },
        "handler": browser_tab_new,
        "security_level": "safe",
    },
    {
        "name": "browser_tab_list",
        "description": "List all open browser tabs with their URLs and titles.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": browser_tab_list,
        "security_level": "safe",
    },
    {
        "name": "browser_tab_switch",
        "description": "Switch to a different browser tab by index (1-based).",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "Tab index to switch to (1-based)"},
            },
            "required": ["index"],
        },
        "handler": browser_tab_switch,
        "security_level": "safe",
    },
    {
        "name": "browser_close",
        "description": "Close the browser session and free resources.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": browser_close,
        "security_level": "safe",
    },
]


def get_browser_tools() -> List[Dict[str, Any]]:
    """Get all browser tool definitions for MCP server registration."""
    return BROWSER_TOOLS


async def execute_browser_tool(tool_name: str, args: Dict[str, Any], session_id: Optional[str] = None) -> Dict[str, Any]:
    """Execute a browser tool by name."""
    if session_id:
        set_session_id(session_id)

    for tool in BROWSER_TOOLS:
        if tool["name"] == tool_name:
            handler = tool["handler"]
            return await handler(**args)

    return {
        "content": [{
            "type": "text",
            "text": f"Unknown browser tool: {tool_name}",
        }]
    }
