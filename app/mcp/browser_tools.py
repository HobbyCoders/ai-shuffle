"""
MCP Tools for web browser capabilities.

Provides tools that Claude can call to control a headless browser, including:
- Navigation (URLs, back, forward)
- Page interaction (click, type, fill, select)
- Content extraction (readable text, accessibility tree, interactive elements)
- Screenshots (explicit opt-in for visual verification)
- Tab management
- JavaScript evaluation

Tools return accessibility tree snapshots by default (token-efficient, semantic)
instead of screenshots. The AI can explicitly call browser_screenshot when
visual context is needed.

Each tool uses Playwright for headless Chromium browser automation.
"""

import contextvars
import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.mcp.browser_session import browser_session_manager, BrowserSession

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

    # Try to cut at a paragraph boundary
    truncated = text[:max_chars]
    last_double_newline = truncated.rfind("\n\n")
    if last_double_newline > max_chars * 0.5:
        truncated = truncated[:last_double_newline]
    else:
        # Fall back to sentence boundary
        last_period = truncated.rfind(". ")
        if last_period > max_chars * 0.5:
            truncated = truncated[:last_period + 1]

    return truncated + f"\n\n[... truncated, {len(text) - len(truncated)} chars remaining]"


def _escape_css_string(value: str) -> str:
    """Escape a string for safe use inside a CSS attribute selector value.

    Escapes backslashes, quotes, and other characters that are significant
    inside CSS ``[attr="value"]`` selectors so that user-supplied input
    cannot break out of the quoted context.
    """
    # Backslash must be escaped first to avoid double-escaping
    value = value.replace("\\", "\\\\")
    value = value.replace('"', '\\"')
    value = value.replace("'", "\\'")
    value = value.replace("\n", "\\n")
    value = value.replace("\r", "\\r")
    value = value.replace("\t", "\\t")
    # Null bytes
    value = value.replace("\0", "\\0 ")
    return value


def _clean_text(text: str) -> str:
    """Clean extracted text: collapse whitespace, remove empty lines."""
    # Collapse multiple spaces
    text = re.sub(r"[ \t]+", " ", text)
    # Collapse multiple newlines (keep max 2)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip leading/trailing whitespace per line
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

    # Add relevant boolean properties
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


async def _get_session() -> BrowserSession:
    """Get or create browser session for current chat."""
    session_id = get_session_id()
    return await browser_session_manager.get_or_create_session(session_id)


# ============================================================================
# Browser Tool Implementations
# ============================================================================

async def browser_navigate(url: str) -> Dict[str, Any]:
    """Navigate to a URL and return page info with accessibility tree."""
    try:
        session = await _get_session()

        # Ensure URL has scheme
        if not url.startswith(("http://", "https://", "file://")):
            url = "https://" + url

        response = await session.page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Wait a bit for dynamic content
        await session.page.wait_for_timeout(1000)

        title = await session.get_title()
        status = response.status if response else None

        # Get brief content summary
        try:
            body_text = await session.page.inner_text("body")
            summary = _truncate_text(_clean_text(body_text), max_chars=500)
        except Exception:
            summary = ""

        # Get accessibility tree for semantic understanding
        a11y = await session.get_accessibility_tree()
        a11y_text = _a11y_to_text(a11y)

        return _text_result({
            "success": True,
            "url": session.current_url,
            "title": title,
            "status": status,
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
        screenshot = await session.take_screenshot(full_page=full_page)

        return _text_and_image_result({
            "success": True,
            "url": session.current_url,
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
            # Click by element index from browser_get_elements
            elements = await _get_interactive_elements(session.page)
            if element_index < 1 or element_index > len(elements):
                return _error_result(f"Element index {element_index} out of range (1-{len(elements)})")
            el = elements[element_index - 1]
            await el["locator"].click(timeout=5000)
            clicked = f"element [{element_index}]"
        elif text:
            # Click by visible text
            await session.page.get_by_text(text, exact=False).first.click(timeout=5000)
            clicked = f'text "{text}"'
        elif selector:
            await session.page.click(selector, timeout=5000)
            clicked = f'selector "{selector}"'
        elif x is not None and y is not None:
            await session.page.mouse.click(x, y)
            clicked = f"coordinates ({x}, {y})"
        else:
            return _error_result("Provide selector, text, element_index, or x/y coordinates")

        # Wait for potential navigation or content change
        await session.page.wait_for_timeout(500)

        title = await session.get_title()
        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "clicked": clicked,
            "url": session.current_url,
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
            await session.page.fill(selector, text, timeout=5000)
            target = f'selector "{selector}"'
        else:
            await session.page.keyboard.type(text)
            target = "focused element"

        if press_enter:
            await session.page.keyboard.press("Enter")

        await session.page.wait_for_timeout(300)
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

        pixels = amount * 300  # ~300px per scroll unit
        if direction == "up":
            pixels = -pixels

        await session.page.evaluate(f"window.scrollBy(0, {pixels})")
        await session.page.wait_for_timeout(300)

        # Get scroll position info
        scroll_info = await session.page.evaluate("""
            () => ({
                scrollY: window.scrollY,
                scrollHeight: document.documentElement.scrollHeight,
                clientHeight: document.documentElement.clientHeight,
            })
        """)

        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "direction": direction,
            "scroll_y": scroll_info["scrollY"],
            "page_height": scroll_info["scrollHeight"],
            "viewport_height": scroll_info["clientHeight"],
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Scroll failed: {str(e)}")


async def browser_get_content(selector: Optional[str] = None) -> Dict[str, Any]:
    """Get readable text content from the page (context-conscious, not raw HTML)."""
    try:
        session = await _get_session()

        target = selector or "body"
        try:
            raw_text = await session.page.inner_text(target)
        except Exception:
            # Fallback: try text_content
            raw_text = await session.page.text_content(target) or ""

        cleaned = _clean_text(raw_text)
        content = _truncate_text(cleaned, max_chars=4000)

        title = await session.get_title()

        return _text_result({
            "success": True,
            "url": session.current_url,
            "title": title,
            "content": content,
            "total_length": len(cleaned),
            "truncated": len(cleaned) > 4000,
        })
    except Exception as e:
        return _error_result(f"Content extraction failed: {str(e)}")


async def _get_interactive_elements(page) -> List[Dict[str, Any]]:
    """Query interactive elements and return structured list."""
    elements = []
    selectors = "a, button, input, select, textarea, [role='button'], [role='link'], [role='tab']"

    locators = page.locator(selectors)
    count = await locators.count()

    # Batch-retrieve all element attributes in a single page.evaluate() call
    # to avoid hundreds of cross-process IPC round-trips.
    cap = min(count, 50)
    try:
        all_attrs = await page.evaluate("""
            (selector) => {
                const nodes = document.querySelectorAll(selector);
                const results = [];
                const limit = Math.min(nodes.length, 50);
                for (let i = 0; i < limit; i++) {
                    const el = nodes[i];
                    // Quick visibility check (not perfect but avoids IPC)
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
                    });
                }
                return results;
            }
        """, selectors)
    except Exception:
        all_attrs = []

    for i, attrs in enumerate(all_attrs):
        loc = locators.nth(i)
        elements.append({
            "locator": loc,
            "tag": attrs["tag"],
            "type": attrs["type"],
            "label": attrs["label"],
            "href": attrs["href"],
            "role": attrs["role"],
            "placeholder": attrs["placeholder"],
            "value": attrs["value"],
            "option_count": attrs["optionCount"],
        })

    return elements


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
        return f'[{index}] select "{label}" ({el["option_count"]} options)'
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


async def browser_get_elements(selector: Optional[str] = None) -> Dict[str, Any]:
    """Get interactive elements on the page as a numbered list for easy reference."""
    try:
        session = await _get_session()
        elements = await _get_interactive_elements(session.page)

        formatted = []
        for i, el in enumerate(elements, 1):
            formatted.append(_format_element(i, el))

        title = await session.get_title()

        return _text_result({
            "success": True,
            "url": session.current_url,
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
        await session.page.go_back(wait_until="domcontentloaded", timeout=10000)
        await session.page.wait_for_timeout(500)

        title = await session.get_title()
        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "url": session.current_url,
            "title": title,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Back navigation failed: {str(e)}")


async def browser_forward() -> Dict[str, Any]:
    """Navigate forward in browser history."""
    try:
        session = await _get_session()
        await session.page.go_forward(wait_until="domcontentloaded", timeout=10000)
        await session.page.wait_for_timeout(500)

        title = await session.get_title()
        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "url": session.current_url,
            "title": title,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Forward navigation failed: {str(e)}")


async def browser_evaluate(script: str) -> Dict[str, Any]:
    """Execute JavaScript in the browser and return the result."""
    try:
        session = await _get_session()
        result = await session.page.evaluate(script)

        # Serialize result
        try:
            result_str = json.dumps(result, indent=2, default=str)
        except (TypeError, ValueError):
            result_str = str(result)

        return _text_result({
            "success": True,
            "result": result_str,
            "url": session.current_url,
        })
    except Exception as e:
        return _error_result(f"JavaScript evaluation failed: {str(e)}")


async def browser_fill(label: str, value: str) -> Dict[str, Any]:
    """Fill a form field identified by its label text."""
    try:
        session = await _get_session()

        # Try multiple strategies to find the input by label
        filled = False

        # Strategy 1: Use label association
        try:
            await session.page.get_by_label(label).fill(value, timeout=3000)
            filled = True
        except Exception:
            pass

        # Strategy 2: Use placeholder
        if not filled:
            try:
                await session.page.get_by_placeholder(label).fill(value, timeout=3000)
                filled = True
            except Exception:
                pass

        # Strategy 3: Use aria-label (escape user input for CSS selector safety)
        if not filled:
            try:
                escaped_label = _escape_css_string(label)
                await session.page.locator(f'[aria-label="{escaped_label}"]').fill(value, timeout=3000)
                filled = True
            except Exception:
                pass

        # Strategy 4: Use name attribute (escape user input for CSS selector safety)
        if not filled:
            try:
                escaped_label = _escape_css_string(label)
                await session.page.locator(f'[name="{escaped_label}"]').fill(value, timeout=3000)
                filled = True
            except Exception:
                pass

        if not filled:
            return _error_result(f'Could not find form field with label "{label}". Try browser_get_elements to see available fields.')

        await session.page.wait_for_timeout(300)
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

        # Try by value first, then by label
        try:
            await session.page.select_option(selector, value=value, timeout=5000)
        except Exception:
            await session.page.select_option(selector, label=value, timeout=5000)

        await session.page.wait_for_timeout(300)
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
            await session.page.wait_for_selector(
                selector,
                state=state,
                timeout=timeout,
            )
            waited_for = f'selector "{selector}" to be {state}'
        else:
            await session.page.wait_for_load_state("networkidle", timeout=timeout)
            waited_for = "network idle"

        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "waited_for": waited_for,
            "url": session.current_url,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Wait failed: {str(e)}")


async def browser_tab_new(url: Optional[str] = None) -> Dict[str, Any]:
    """Open a new browser tab."""
    try:
        session = await _get_session()
        new_page = await session.context.new_page()
        session._pages.append(new_page)
        session.page = new_page

        if url:
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            await new_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await new_page.wait_for_timeout(1000)

        title = await session.get_title()
        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "tab_count": len(session._pages),
            "active_tab": len(session._pages),
            "url": session.current_url,
            "title": title,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Tab creation failed: {str(e)}")


async def browser_tab_list() -> Dict[str, Any]:
    """List all open browser tabs."""
    try:
        session = await _get_session()
        tabs = []
        active_index = None

        for i, page in enumerate(session._pages):
            try:
                url = page.url
                title = await page.title()
                is_active = page == session.page
                if is_active:
                    active_index = i + 1
                tabs.append({
                    "index": i + 1,
                    "url": url,
                    "title": title,
                    "active": is_active,
                })
            except Exception:
                tabs.append({
                    "index": i + 1,
                    "url": "unknown",
                    "title": "unknown",
                    "active": False,
                })

        return _text_result({
            "success": True,
            "tab_count": len(tabs),
            "active_tab": active_index,
            "tabs": tabs,
        })
    except Exception as e:
        return _error_result(f"Tab listing failed: {str(e)}")


async def browser_tab_switch(index: int) -> Dict[str, Any]:
    """Switch to a tab by index (1-based)."""
    try:
        session = await _get_session()

        if index < 1 or index > len(session._pages):
            return _error_result(f"Tab index {index} out of range (1-{len(session._pages)})")

        session.page = session._pages[index - 1]
        await session.page.bring_to_front()

        title = await session.get_title()
        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "active_tab": index,
            "url": session.current_url,
            "title": title,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Tab switch failed: {str(e)}")


async def browser_close() -> Dict[str, Any]:
    """Close the browser session."""
    try:
        session_id = get_session_id()
        await browser_session_manager.close_session(session_id)

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
        a11y = await session.get_accessibility_tree()

        return _text_result({
            "success": True,
            "url": session.current_url,
            "title": title,
            "accessibility_tree": _a11y_to_text(a11y),
        })
    except Exception as e:
        return _error_result(f"Observe failed: {str(e)}")


# ============================================================================
# Tool Registry (follows phone_tools.py pattern)
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
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "handler": browser_observe,
        "security_level": "safe",
    },
    {
        "name": "browser_click",
        "description": "Click an element on the page. Use element_index (from browser_get_elements), visible text, CSS selector, or x/y coordinates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_index": {
                    "type": "integer",
                    "description": "Element index from browser_get_elements (1-based)",
                },
                "text": {
                    "type": "string",
                    "description": "Visible text of the element to click",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the element to click",
                },
                "x": {
                    "type": "integer",
                    "description": "X coordinate to click",
                },
                "y": {
                    "type": "integer",
                    "description": "Y coordinate to click",
                },
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
                "text": {
                    "type": "string",
                    "description": "Text to type",
                },
                "selector": {
                    "type": "string",
                    "description": "Optional CSS selector to type into (uses fill for immediate input). If omitted, types into the currently focused element.",
                },
                "press_enter": {
                    "type": "boolean",
                    "description": "Press Enter after typing (default: false)",
                    "default": False,
                },
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
                "direction": {
                    "type": "string",
                    "description": "Scroll direction",
                    "enum": ["up", "down"],
                    "default": "down",
                },
                "amount": {
                    "type": "integer",
                    "description": "Number of scroll units (1 unit \u2248 300px, default: 3)",
                    "default": 3,
                },
            },
        },
        "handler": browser_scroll,
        "security_level": "safe",
    },
    {
        "name": "browser_get_content",
        "description": "Get the readable text content of the page. Returns clean text (not raw HTML), truncated to ~4000 chars. Use selector to extract content from a specific element.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "Optional CSS selector to extract content from (default: entire page body)",
                },
            },
        },
        "handler": browser_get_content,
        "security_level": "safe",
    },
    {
        "name": "browser_get_elements",
        "description": "Get a numbered list of interactive elements on the page (links, buttons, inputs, selects). Use the returned index numbers with browser_click(element_index=N) to interact.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "Optional CSS selector to scope the search",
                },
            },
        },
        "handler": browser_get_elements,
        "security_level": "safe",
    },
    {
        "name": "browser_back",
        "description": "Navigate back in browser history.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "handler": browser_back,
        "security_level": "safe",
    },
    {
        "name": "browser_forward",
        "description": "Navigate forward in browser history.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "handler": browser_forward,
        "security_level": "safe",
    },
    {
        "name": "browser_evaluate",
        "description": "Execute JavaScript code in the browser and return the result. Use for advanced interactions or data extraction.",
        "input_schema": {
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "JavaScript code to execute in the browser context",
                },
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
                "label": {
                    "type": "string",
                    "description": "Label text, placeholder, aria-label, or name of the form field",
                },
                "value": {
                    "type": "string",
                    "description": "Value to fill into the field",
                },
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
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the select element",
                },
                "value": {
                    "type": "string",
                    "description": "Option value or visible text to select",
                },
            },
            "required": ["selector", "value"],
        },
        "handler": browser_select,
        "security_level": "confirm",
    },
    {
        "name": "browser_wait",
        "description": "Wait for a condition \u2014 an element to appear/disappear, or network to become idle.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector to wait for (omit to wait for network idle)",
                },
                "state": {
                    "type": "string",
                    "description": "State to wait for",
                    "enum": ["visible", "hidden", "attached", "detached"],
                    "default": "visible",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum wait time in milliseconds (default: 10000)",
                    "default": 10000,
                },
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
                "url": {
                    "type": "string",
                    "description": "Optional URL to open in the new tab",
                },
            },
        },
        "handler": browser_tab_new,
        "security_level": "safe",
    },
    {
        "name": "browser_tab_list",
        "description": "List all open browser tabs with their URLs and titles.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "handler": browser_tab_list,
        "security_level": "safe",
    },
    {
        "name": "browser_tab_switch",
        "description": "Switch to a different browser tab by index (1-based).",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "integer",
                    "description": "Tab index to switch to (1-based, from browser_tab_list)",
                },
            },
            "required": ["index"],
        },
        "handler": browser_tab_switch,
        "security_level": "safe",
    },
    {
        "name": "browser_close",
        "description": "Close the browser session and free resources.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "handler": browser_close,
        "security_level": "safe",
    },
]


def get_browser_tools() -> List[Dict[str, Any]]:
    """Get all browser tool definitions for MCP server registration."""
    return BROWSER_TOOLS


async def execute_browser_tool(tool_name: str, args: Dict[str, Any], session_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Execute a browser tool by name.

    Args:
        tool_name: Name of the tool to execute
        args: Tool arguments
        session_id: Chat session ID for browser session management

    Returns:
        Tool result dict
    """
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
