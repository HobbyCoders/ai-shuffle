import type { Page, ElementHandle } from 'playwright-core';
import { randomUUID } from 'node:crypto';
import type { BrowserEditor } from './workspace-protocol.js';
import { validatePaths } from './tools/upload.js';

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
  const handle = await page.evaluateHandle((id) => {
    const host = window as typeof window & { __aisEditors?: { nodes: Map<string, HTMLInputElement> } };
    const el = host.__aisEditors?.nodes.get(id);
    if (!el?.isConnected || el.disabled || el.type !== 'file') throw new Error('File field changed. Choose the files again.');
    return el;
  }, args.field as string);
  try {
    const el = handle.asElement();
    if (!el) throw new Error('File field unavailable');
    await el.setInputFiles(validated.paths, { timeout: 10000 });
    await page.evaluate(input => {
      const host = window as typeof window & { __aisEditors?: { pending?: Element } };
      if (host.__aisEditors?.pending === input) delete host.__aisEditors.pending;
    }, el).catch(() => { /* onchange may already have navigated after a successful upload */ });
  } finally { await handle.dispose(); }
}

/** Page-local handles: a detached/replaced node can never receive a stale edit. */
export async function readEditors(page: Page): Promise<BrowserEditor[]> {
  return page.evaluate((seed) => {
    type Field = HTMLInputElement | HTMLTextAreaElement;
    const host = window as typeof window & { __aisEditors?: { ids: WeakMap<Element, string>; nodes: Map<string, Field>; initial: WeakMap<Element, string>; pending?: HTMLInputElement } };
    const registry: NonNullable<typeof host.__aisEditors> = host.__aisEditors ??= { ids: new WeakMap(), nodes: new Map(), initial: new WeakMap() };
    registry.nodes.clear();
    const fields = []; let budget = 24000;
    const candidates = Array.from(document.querySelectorAll<Field>('input, textarea'));
    if (registry.pending?.isConnected) {
      const index = candidates.indexOf(registry.pending);
      if (index >= 0) candidates.splice(index, 1);
      candidates.unshift(registry.pending); // include iframe choosers and prioritize over the field cap
    }
    for (const el of candidates) {
      if (fields.length >= 32) break;
      const type = el instanceof HTMLTextAreaElement ? 'textarea' : el.type;
      if (!['text', 'search', 'email', 'url', 'tel', 'password', 'textarea', 'file'].includes(type) || el.disabled || el.readOnly) continue;
      const anchor = type === 'file' && (!el.checkVisibility() || el.getBoundingClientRect().width < 1)
        ? Array.from(el.labels ?? []).find(label => label.checkVisibility()) ?? el : el;
      const chooser = type === 'file' && registry.pending === el;
      // Large forms often retain thousands of hidden fields. Skip them before
      // asking for geometry, while preserving explicit hidden file choosers.
      if (!chooser && !anchor.checkVisibility()) continue;
      const rect = chooser ? new DOMRect(16, 16, Math.max(240, innerWidth - 32), 44) : anchor.getBoundingClientRect();
      if (rect.width < 1 || rect.height < 1 || rect.bottom <= 0 || rect.right <= 0 || rect.top >= innerHeight || rect.left >= innerWidth) continue;
      // Do not put password contents into shared workspace state.
      const value = type === 'password' ? '' : type === 'file' ? Array.from((el as HTMLInputElement).files ?? []).map(f => f.name).join(', ') : el.value;
      if (value.length > 16384 || value.length > budget) continue;
      budget -= value.length;
      let id = registry.ids.get(el);
      if (!id) { id = `${seed}-${fields.length}`; registry.ids.set(el, id); }
      registry.nodes.set(id, el);
      fields.push({ id, type, value, ...(type === 'file' ? { chooser, accept: (el as HTMLInputElement).accept.slice(0, 1024), multiple: (el as HTMLInputElement).multiple } : {}), label: (el.getAttribute('aria-label') || el.labels?.[0]?.textContent || el.placeholder || type).slice(0, 256), x: rect.x, y: rect.y, width: rect.width, height: rect.height });
    }
    return fields;
  }, randomUUID());
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
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertReplacementText', data: null }));
  }, args);
}
