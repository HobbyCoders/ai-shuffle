import type { Page, ElementHandle } from 'playwright-core';
import { randomUUID } from 'node:crypto';
import type { BrowserChoices, BrowserEditor } from './workspace-protocol.js';
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
    if (!el?.isConnected || el.matches(':disabled') || el.type !== 'file') throw new Error('File field changed. Choose the files again.');
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
    type Field = HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
    const host = window as typeof window & { __aisEditors?: { ids: WeakMap<Element, string>; nodes: Map<string, Field>; initial: WeakMap<Element, string>; pending?: HTMLInputElement } };
    const registry: NonNullable<typeof host.__aisEditors> = host.__aisEditors ??= { ids: new WeakMap(), nodes: new Map(), initial: new WeakMap() };
    registry.nodes.clear();
    // Controls whose popup Chrome draws outside the page (so outside the
    // screencast): the panel shows its own picker for these instead.
    const PICKERS = ['select', 'date', 'time', 'datetime-local', 'month', 'week'];
    // The focused control, followed through open shadow roots.
    let focusedEl: Element | null = document.activeElement;
    while (focusedEl?.shadowRoot?.activeElement) focusedEl = focusedEl.shadowRoot.activeElement;
    const fields = []; let budget = 24000;
    // The page's own clock zone: the panel's Today/Now must match the page's, not the viewer's.
    const zone = (() => { try { const z = Intl.DateTimeFormat().resolvedOptions().timeZone; return z && z.length <= 64 ? z : ''; } catch { return ''; } })();
    // querySelectorAll stops at shadow boundaries. Reddit's auth fields (and
    // many other web components) are native inputs inside open shadow roots.
    function* inputs(root: Document | ShadowRoot): Generator<Field> {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const el = node as Element;
        if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) yield el;
        if (el.shadowRoot) yield* inputs(el.shadowRoot);
      }
    }
    // Shadow labels commonly contain slots, whose textContent omits the actual
    // assigned label. Resolve only label text; never collect a field's value.
    function labelText(node: Node): string {
      if (node instanceof Element && node.getAttribute('aria-hidden') === 'true') return '';
      // A label wrapping its select would otherwise read out every option.
      if (node instanceof HTMLSelectElement) return '';
      if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? '';
      const assigned = node instanceof HTMLSlotElement ? node.assignedNodes({ flatten: true }) : [];
      return Array.from(assigned.length ? assigned : node.childNodes, labelText).join('');
    }
    function labelFor(el: Field): string {
      const root = el.getRootNode() as Document | ShadowRoot;
      const labelled = (el.getAttribute('aria-labelledby') ?? '').split(/\s+/)
        .map(id => root.getElementById(id)).filter((node): node is HTMLElement => !!node)
        .map(labelText).join(' ').trim();
      return (el.getAttribute('aria-label') || labelled || (el.labels?.[0] && labelText(el.labels[0]).trim()) || ('placeholder' in el ? el.placeholder : '') || el.type)
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
      const type = el instanceof HTMLTextAreaElement ? 'textarea' : el instanceof HTMLSelectElement ? 'select' : el.type;
      const picker = PICKERS.includes(type);
      // :disabled also covers a control inside <fieldset disabled>.
      if (!['text', 'search', 'email', 'url', 'tel', 'password', 'textarea', 'file', ...PICKERS].includes(type) || el.matches(':disabled') || ('readOnly' in el && el.readOnly)) continue;
      // A list box (multiple or size > 1) is drawn in the page itself: clicks reach it.
      if (el instanceof HTMLSelectElement && (el.multiple || el.size > 1)) continue;
      // Rich text widgets need the page's real pointer and keyboard handlers.
      if (type !== 'file' && !picker && (
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
      // A select reports the text of its chosen option, which is what it shows.
      const value = type === 'password' ? '' : type === 'file' ? Array.from((el as HTMLInputElement).files ?? []).map(f => f.name).join(', ')
        : el instanceof HTMLSelectElement ? (el.selectedOptions[0]?.label ?? '').replace(/\s+/g, ' ').trim().slice(0, 256) : el.value;
      if (value.length > 16384 || value.length > budget) continue;
      budget -= value.length;
      let id = registry.ids.get(el);
      if (!id) { id = `${seed}-${fields.length}`; registry.ids.set(el, id); }
      registry.nodes.set(id, el);
      // Split one-time-code boxes reformat themselves and move focus on every
      // digit. Those take real keystrokes, so the page's own handlers run.
      const keys = type !== 'file' && type !== 'textarea' && !picker && ((el as HTMLInputElement).autocomplete === 'one-time-code' || ((el as HTMLInputElement).maxLength > 0 && (el as HTMLInputElement).maxLength <= 2));
      // Pickers draw no editable text in the panel: no typography needed.
      const css = typography && !picker ? getComputedStyle(el) : null;
      // Date/time limits travel as the page wrote them (the panel parses them).
      // Without min, HTML steps count from the value attribute: that is `base`.
      const limits: Record<string, string> = {};
      if (picker && el instanceof HTMLInputElement) for (const [name, attr] of [['min', 'min'], ['max', 'max'], ['step', 'step'], ['base', 'value']] as const) {
        const v = el.getAttribute(attr)?.trim();
        if (v && v.length <= 64) limits[name] = v;
      }
      if (picker && type !== 'select' && zone) limits.zone = zone;
      const style = css ? {
        fontSize: parseFloat(css.fontSize), lineHeight: css.lineHeight === 'normal' ? null : parseFloat(css.lineHeight),
        paddingLeft: parseFloat(css.paddingLeft), paddingTop: parseFloat(css.paddingTop), fontFamily: css.fontFamily.slice(0, 200),
        borderLeft: parseFloat(css.borderLeftWidth), borderTop: parseFloat(css.borderTopWidth),
      } : undefined;
      const validStyle = style && Object.values(style).every(v => typeof v === 'string' || v === null || (Number.isFinite(v) && v >= 0 && v <= 1000));
      fields.push({ id, type, value, ...limits, ...(picker && el === focusedEl ? { focused: true } : {}), ...(validStyle ? { style } : {}), ...(keys ? { keys } : {}), ...(type === 'file' ? { chooser, accept: (el as HTMLInputElement).accept.slice(0, 1024), multiple: (el as HTMLInputElement).multiple } : {}), label: labelFor(el), x: rect.x, y: rect.y, width: rect.width, height: rect.height });
    }
    return fields;
  }, { seed: randomUUID(), typography });
}

/**
 * Most options a select's list carries to the panel, and their total label
 * length in code points, group labels included: the server counts the same way.
 */
export const MAX_CHOICES = 1000;
export const CHOICE_TEXT_BUDGET = 24000;
/** Longest option or group label, in code points. */
export const CHOICE_LABEL = 200;

/**
 * A select's options for the panel's own list: labels, groups and disabled
 * state, read when the viewer opens it. Option values never leave the page;
 * the panel picks by index and sends the label it showed, so a list that
 * changed in between is refused rather than set to a different option.
 */
export async function readChoices(page: Page, args: Record<string, unknown>): Promise<BrowserChoices> {
  return page.evaluate(({ field, max, budget, labelMax }) => {
    // A label as the panel shows it: control characters and unpaired
    // surrogates dropped, whitespace collapsed, at most `max` code points.
    // Each kept code point then costs at most 4 bytes of JSON, so a full list
    // stays far inside the report body limit. Same code in editField's check.
    const clean = (text: string, max: number) => {
      const points = Array.from(text.replace(/[\u0000-\u001f\u007f-\u009f]|[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/g, ' ').replace(/\s+/g, ' ').trim());
      return points.length > max ? points.slice(0, max).join('').trimEnd() : points.join('');
    };
    const host = window as typeof window & { __aisEditors?: { nodes: Map<string, Element> } };
    const el = host.__aisEditors?.nodes.get(field);
    if (!(el instanceof HTMLSelectElement) || !el.isConnected || el.matches(':disabled')) throw new Error('This list changed. Tap it again.');
    const groups: string[] = [];
    const groupIndex = new Map<HTMLOptGroupElement, number>();
    const options: { label: string; disabled: boolean; group?: number }[] = [];
    let left = budget, truncated = false;
    const size = (text: string) => Array.from(text).length;
    for (const option of Array.from(el.options)) {
      if (options.length >= max) { truncated = true; break; }
      const parent = option.parentElement instanceof HTMLOptGroupElement ? option.parentElement : null;
      const label = clean(option.label, labelMax);
      // A new group's label is paid for together with its first option.
      const newGroup = parent && !groupIndex.has(parent) ? clean(parent.label, labelMax) : null;
      const cost = size(label) + (newGroup === null ? 0 : size(newGroup));
      if (cost > left) { truncated = true; break; }
      left -= cost;
      if (parent && newGroup !== null) { groupIndex.set(parent, groups.length); groups.push(newGroup); }
      const group = parent ? groupIndex.get(parent) : undefined;
      // A hidden option is not offered by the page's own popup either.
      const disabled = option.disabled || !!parent?.disabled || option.hidden;
      options.push({ label, disabled, ...(group === undefined ? {} : { group }) });
    }
    const selected = el.selectedIndex < options.length ? el.selectedIndex : -1;
    return { selected, groups, options, ...(truncated ? { truncated } : {}) };
  }, { field: args.field as string, max: MAX_CHOICES, budget: CHOICE_TEXT_BUDGET, labelMax: CHOICE_LABEL });
}

export async function editField(page: Page, args: Record<string, unknown>): Promise<void> {
  await page.evaluate((a) => {
    type Field = HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
    const host = window as typeof window & { __aisEditors?: { nodes: Map<string, Field>; initial: WeakMap<Element, string>; pending?: HTMLInputElement } };
    const el = host.__aisEditors?.nodes.get(a.field as string);
    const PICKERS = ['date', 'time', 'datetime-local', 'month', 'week'];
    const picker = el instanceof HTMLSelectElement || (el instanceof HTMLInputElement && PICKERS.includes(el.type));
    if (!el?.isConnected || el.matches(':disabled') || ('readOnly' in el && el.readOnly) || (el instanceof HTMLInputElement && el.type === 'file')) throw new Error('Text field changed. Tap the field again.');
    const initial = host.__aisEditors!.initial;
    if (a.action === 'set') {
      // The panel's own list/calendar stands in for the popup Chrome would draw
      // outside the page. Like that popup, a choice focuses the control, sets
      // it and fires input then change; picking the current value is no change.
      if (!picker) throw new Error('Text field changed. Tap the field again.');
      el.focus({ preventScroll: true });
      initial.delete(el); // a later blur must not report this change twice
      const before = el.value;
      if (el instanceof HTMLSelectElement) {
        const option = el.options[a.index as number];
        // The same label cleaning as readChoices: the panel sends back the
        // label it showed, so a list the page refilled since is not set to
        // whatever now sits at that index.
        const clean = (text: string, max: number) => {
          const points = Array.from(text.replace(/[\u0000-\u001f\u007f-\u009f]|[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/g, ' ').replace(/\s+/g, ' ').trim());
          return points.length > max ? points.slice(0, max).join('').trimEnd() : points.join('');
        };
        if (!option || clean(option.label, a.labelMax as number) !== a.label) throw new Error('This list changed. Open it again.');
        if (option.disabled || option.hidden || (option.parentElement instanceof HTMLOptGroupElement && option.parentElement.disabled))
          throw new Error('That option is unavailable. Open the list again.');
        if (el.selectedIndex === a.index) return;
        el.selectedIndex = a.index as number;
      } else {
        // Native setter works with controlled React/Vue inputs. A malformed
        // value is sanitized to '' by the browser: refuse it and restore.
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
        setter.call(el, a.value as string);
        if (a.value !== '' && el.value === '') { setter.call(el, before); throw new Error("The page didn't accept that value."); }
        if (el.value === before) return;
      }
      el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return;
    }
    if (a.action === 'blur') {
      if (initial.has(el) && initial.get(el) !== el.value) el.dispatchEvent(new Event('change', { bubbles: true }));
      initial.delete(el);
      el.blur(); return;
    }
    if (!initial.has(el)) initial.set(el, el.value);
    el.focus({ preventScroll: true });
    if (a.action === 'focus') return;
    // Picker values only change through 'set': never through a text replacement.
    if (picker) throw new Error('Text field changed. Tap the field again.');
    const value = a.value as string;
    // Native setter works with controlled React/Vue inputs; page handlers receive
    // input events, not arbitrary executable client-provided JavaScript.
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value')!.set!.call(el, value);
    try { (el as HTMLInputElement).setSelectionRange(a.start as number, a.end as number); } catch { /* email inputs have no selection API */ }
    // Native input events cross open shadow roots so host/framework listeners
    // receive edits just as they do with physical typing.
    el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, inputType: 'insertReplacementText', data: null }));
  }, { ...args, labelMax: CHOICE_LABEL } as Record<string, unknown>);
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
