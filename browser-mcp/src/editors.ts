import type { Page, ElementHandle } from 'playwright-core';
import { randomUUID } from 'node:crypto';
import type { BrowserEditor } from './workspace-protocol.js';
import { validatePaths, uploadTimeoutMs } from './tools/upload.js';
import { raceWithTimeout } from './session.js';

/** Ceiling for locating the chosen file field in the page (the transfer has its own). */
export const FILE_FIELD_TIMEOUT_MS = 5000;

export async function rememberFileChooser(page: Page, input: ElementHandle<Node>): Promise<void> {
  await page.evaluate(node => {
    const el = node as HTMLInputElement;
    if (el.nodeName !== 'INPUT' || el.type !== 'file') throw new Error('Not a file input');
    const host = window as typeof window & { __aisEditors?: { ids: WeakMap<Element, string>; nodes: Map<string, HTMLInputElement | HTMLTextAreaElement>; initial: WeakMap<Element, string>; pending?: HTMLInputElement } };
    const registry: NonNullable<typeof host.__aisEditors> = host.__aisEditors ??= { ids: new WeakMap(), nodes: new Map(), initial: new WeakMap() };
    registry.ids.delete(el); // a new picker invocation gets a new capture identity
    registry.pending = el;
  }, input);
}

/** Deliver only app-staged files into the original, still-connected input. */
export async function uploadField(page: Page, args: Record<string, unknown>): Promise<void> {
  const validated = validatePaths(args.paths as string[], process.env.AIS_BROWSER_PANEL_UPLOAD_DIR);
  if ('error' in validated) throw new Error('Selected upload files are unavailable or outside the upload directory');
  const pending = page.evaluateHandle((id) => {
    const host = window as typeof window & { __aisEditors?: { nodes: Map<string, HTMLInputElement> } };
    const el = host.__aisEditors?.nodes.get(id);
    if (!el?.isConnected || el.disabled || el.type !== 'file') throw new Error('File field changed. Choose the files again.');
    return el;
  }, args.field as string);
  // A handle that arrives after the deadline is released, not leaked.
  const handle = await raceWithTimeout(pending, FILE_FIELD_TIMEOUT_MS, 'file field lookup timed out').catch(async (err) => {
    void pending.then(late => late.dispose(), () => {});
    throw err;
  });
  try {
    const el = handle.asElement();
    if (!el) throw new Error('File field unavailable');
    await el.setInputFiles(validated.paths, { timeout: uploadTimeoutMs(validated.totalBytes) });
    await page.evaluate(input => {
      const host = window as typeof window & { __aisEditors?: { pending?: Element } };
      if (host.__aisEditors?.pending === input) delete host.__aisEditors.pending;
    }, el).catch(() => { /* onchange may already have navigated after a successful upload */ });
  } finally { await handle.dispose(); }
}

/** Page-local handles: a detached/replaced node can never receive a stale edit. */
export async function readEditors(page: Page, typography = process.env._AIS_BROWSER_EDITOR_STYLE === '1'): Promise<BrowserEditor[]> {
  return page.evaluate(({ seed, typography }) => {
    type Field = HTMLInputElement | HTMLTextAreaElement;
    const host = window as typeof window & { __aisEditors?: { ids: WeakMap<Element, string>; nodes: Map<string, Field>; initial: WeakMap<Element, string>; pending?: HTMLInputElement } };
    const registry: NonNullable<typeof host.__aisEditors> = host.__aisEditors ??= { ids: new WeakMap(), nodes: new Map(), initial: new WeakMap() };
    registry.nodes.clear();
    const fields = []; let budget = 24000;
    // querySelectorAll stops at shadow boundaries. Reddit's auth fields (and
    // many other web components) are native inputs inside open shadow roots.
    function* inputs(root: Document | ShadowRoot): Generator<Field> {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const el = node as Element;
        if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) yield el;
        if (el.shadowRoot) yield* inputs(el.shadowRoot);
      }
    }
    // Shadow labels commonly contain slots, whose textContent omits the actual
    // assigned label. Resolve only label text; never collect a field's value.
    function labelText(node: Node): string {
      if (node instanceof Element && node.getAttribute('aria-hidden') === 'true') return '';
      if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? '';
      const assigned = node instanceof HTMLSlotElement ? node.assignedNodes({ flatten: true }) : [];
      return Array.from(assigned.length ? assigned : node.childNodes, labelText).join('');
    }
    function labelFor(el: Field): string {
      const root = el.getRootNode() as Document | ShadowRoot;
      const labelled = (el.getAttribute('aria-labelledby') ?? '').split(/\s+/)
        .map(id => root.getElementById(id)).filter((node): node is HTMLElement => !!node)
        .map(labelText).join(' ').trim();
      return (el.getAttribute('aria-label') || labelled || (el.labels?.[0] && labelText(el.labels[0]).trim()) || el.placeholder || el.type)
        .replace(/\s+/g, ' ').slice(0, 256);
    }
    const candidates = Array.from(inputs(document));
    if (registry.pending?.isConnected) {
      const index = candidates.indexOf(registry.pending);
      if (index >= 0) candidates.splice(index, 1);
      candidates.unshift(registry.pending); // include iframe choosers and prioritize over the field cap
    }
    for (const el of candidates) {
      if (fields.length >= 32) break;
      const type = el instanceof HTMLTextAreaElement ? 'textarea' : el.type;
      if (!['text', 'search', 'email', 'url', 'tel', 'password', 'textarea', 'file'].includes(type) || el.disabled || el.readOnly) continue;
      // Rich text widgets need the page's real pointer and keyboard handlers.
      if (type !== 'file' && (
        el.getAttribute('role')?.toLowerCase().split(/\s+/).includes('combobox') ||
        (el.hasAttribute('aria-haspopup') && el.getAttribute('aria-haspopup')?.toLowerCase() !== 'false') ||
        ['inline', 'list', 'both'].includes(el.getAttribute('aria-autocomplete')?.toLowerCase() ?? '') ||
        (el instanceof HTMLInputElement && el.list instanceof HTMLDataListElement)
      )) continue;
      const anchor = type === 'file' && (!el.checkVisibility() || el.getBoundingClientRect().width < 1)
        ? Array.from(el.labels ?? []).find(label => label.checkVisibility()) ?? el : el;
      const chooser = type === 'file' && registry.pending === el;
      // Large forms often retain thousands of hidden fields. Skip them before
      // asking for geometry, while preserving explicit hidden file choosers.
      if (!chooser && !anchor.checkVisibility()) continue;
      const rect = chooser ? new DOMRect(16, 16, Math.max(240, innerWidth - 32), 44) : anchor.getBoundingClientRect();
      if (rect.width < 1 || rect.height < 1 || rect.bottom <= 0 || rect.right <= 0 || rect.top >= innerHeight || rect.left >= innerWidth) continue;
      if (!chooser) {
        const x = rect.x + rect.width / 2, y = rect.y + rect.height / 2;
        let hit = document.elementFromPoint(x, y);
        // Document hit-testing returns the shadow host, not its inner control.
        while (hit?.shadowRoot) {
          const inner = hit.shadowRoot.elementFromPoint(x, y);
          if (!inner || inner === hit) break;
          hit = inner;
        }
        if (!hit || !(hit === el || el.contains(hit) ||
          Array.from(el.labels ?? []).some(label => label === hit || label.contains(hit)))) continue;
      }
      // Do not put password contents into shared workspace state.
      const value = type === 'password' ? '' : type === 'file' ? Array.from((el as HTMLInputElement).files ?? []).map(f => f.name).join(', ') : el.value;
      if (value.length > 16384 || value.length > budget) continue;
      budget -= value.length;
      let id = registry.ids.get(el);
      if (!id) { id = `${seed}-${fields.length}`; registry.ids.set(el, id); }
      registry.nodes.set(id, el);
      // Split one-time-code boxes reformat themselves and move focus on every
      // digit. Those take real keystrokes, so the page's own handlers run.
      const keys = type !== 'file' && type !== 'textarea' && ((el as HTMLInputElement).autocomplete === 'one-time-code' || (el.maxLength > 0 && el.maxLength <= 2));
      const css = typography ? getComputedStyle(el) : null;
      const style = css ? {
        fontSize: parseFloat(css.fontSize), lineHeight: css.lineHeight === 'normal' ? null : parseFloat(css.lineHeight),
        paddingLeft: parseFloat(css.paddingLeft), paddingTop: parseFloat(css.paddingTop), fontFamily: css.fontFamily.slice(0, 200),
        borderLeft: parseFloat(css.borderLeftWidth), borderTop: parseFloat(css.borderTopWidth),
      } : undefined;
      const validStyle = style && Object.values(style).every(v => typeof v === 'string' || v === null || (Number.isFinite(v) && v >= 0 && v <= 1000));
      fields.push({ id, type, value, ...(validStyle ? { style } : {}), ...(keys ? { keys } : {}), ...(type === 'file' ? { chooser, accept: (el as HTMLInputElement).accept.slice(0, 1024), multiple: (el as HTMLInputElement).multiple } : {}), label: labelFor(el), x: rect.x, y: rect.y, width: rect.width, height: rect.height });
    }
    return fields;
  }, { seed: randomUUID(), typography });
}

export async function editField(page: Page, args: Record<string, unknown>): Promise<void> {
  await page.evaluate((a) => {
    type Field = HTMLInputElement | HTMLTextAreaElement;
    const host = window as typeof window & { __aisEditors?: { nodes: Map<string, Field>; initial: WeakMap<Element, string>; pending?: HTMLInputElement } };
    const el = host.__aisEditors?.nodes.get(a.field as string);
    if (!el?.isConnected || el.disabled || el.readOnly || (el instanceof HTMLInputElement && el.type === 'file')) throw new Error('Text field changed. Tap the field again.');
    const initial = host.__aisEditors!.initial;
    if (a.action === 'blur') {
      if (initial.has(el) && initial.get(el) !== el.value) el.dispatchEvent(new Event('change', { bubbles: true }));
      initial.delete(el);
      el.blur(); return;
    }
    if (!initial.has(el)) initial.set(el, el.value);
    el.focus({ preventScroll: true });
    if (a.action === 'focus') return;
    const value = a.value as string;
    // Native setter works with controlled React/Vue inputs; page handlers receive
    // input events, not arbitrary executable client-provided JavaScript.
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value')!.set!.call(el, value);
    try { el.setSelectionRange(a.start as number, a.end as number); } catch { /* email inputs have no selection API */ }
    // Native input events cross open shadow roots so host/framework listeners
    // receive edits just as they do with physical typing.
    el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, inputType: 'insertReplacementText', data: null }));
  }, args);
}

/** Longest copy a viewer can take in one go (matches the edit limit). */
export const COPY_LIMIT = 16384;

/** The page's current selection, for the viewer's own clipboard. Password fields are refused. */
export async function copySelection(page: Page): Promise<string> {
  const text = await page.evaluate(() => {
    // Follow focus through open shadow roots and same-origin frames to the real field.
    let doc: Document | ShadowRoot = document;
    let active: Element | null = document.activeElement;
    for (;;) {
      if (active?.shadowRoot?.activeElement) { doc = active.shadowRoot; active = active.shadowRoot.activeElement; continue; }
      if (active instanceof HTMLIFrameElement) {
        try { const inner = active.contentDocument; if (inner) { doc = inner; active = inner.activeElement; continue; } } catch { /* cross-origin frame */ }
      }
      break;
    }
    if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) {
      if (active instanceof HTMLInputElement && active.type === 'password') return { refused: true, text: '' };
      try {
        const start = active.selectionStart, end = active.selectionEnd;
        if (start !== null && end !== null && end > start) return { refused: false, text: active.value.slice(start, end) };
      } catch { /* email/number inputs have no selection API */ }
    }
    const selection = 'getSelection' in doc && typeof doc.getSelection === 'function' ? doc.getSelection() : window.getSelection();
    return { refused: false, text: selection?.toString() ?? '' };
  });
  if (text.refused) throw new Error("Password fields can't be copied.");
  if (!text.text) throw new Error('Select some text on the page first.');
  if (text.text.length > COPY_LIMIT) throw new Error(`That selection is too long to copy (limit ${COPY_LIMIT} characters).`);
  return text.text;
}
