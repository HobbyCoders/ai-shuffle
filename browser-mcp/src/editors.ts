import type { Page } from 'playwright-core';
import { randomUUID } from 'node:crypto';
import type { BrowserEditor } from './workspace-protocol.js';

/** Page-local handles: a detached/replaced node can never receive a stale edit. */
export async function readEditors(page: Page): Promise<BrowserEditor[]> {
  return page.evaluate((seed) => {
    type Field = HTMLInputElement | HTMLTextAreaElement;
    const host = window as typeof window & { __aisEditors?: { ids: WeakMap<Element, string>; nodes: Map<string, Field>; initial: WeakMap<Element, string> } };
    const registry = host.__aisEditors ??= { ids: new WeakMap(), nodes: new Map(), initial: new WeakMap() };
    registry.nodes.clear();
    const fields = []; let budget = 24000;
    for (const el of Array.from(document.querySelectorAll<Field>('input, textarea'))) {
      if (fields.length >= 32) break;
      const type = el instanceof HTMLTextAreaElement ? 'textarea' : el.type;
      if (!['text', 'search', 'email', 'url', 'tel', 'password', 'textarea'].includes(type) || el.disabled || el.readOnly) continue;
      const rect = el.getBoundingClientRect();
      if (!el.checkVisibility() || rect.width < 1 || rect.height < 1 || rect.bottom <= 0 || rect.right <= 0 || rect.top >= innerHeight || rect.left >= innerWidth) continue;
      // Do not put password contents into shared workspace state.
      const value = type === 'password' ? '' : el.value;
      if (value.length > 16384 || value.length > budget) continue;
      budget -= value.length;
      let id = registry.ids.get(el);
      if (!id) { id = `${seed}-${fields.length}`; registry.ids.set(el, id); }
      registry.nodes.set(id, el);
      fields.push({ id, type, value, label: (el.getAttribute('aria-label') || el.labels?.[0]?.textContent || el.placeholder || type).slice(0, 256), x: rect.x, y: rect.y, width: rect.width, height: rect.height });
    }
    return fields;
  }, randomUUID());
}

export async function editField(page: Page, args: Record<string, unknown>): Promise<void> {
  await page.evaluate((a) => {
    type Field = HTMLInputElement | HTMLTextAreaElement;
    const host = window as typeof window & { __aisEditors?: { nodes: Map<string, Field>; initial: WeakMap<Element, string> } };
    const el = host.__aisEditors?.nodes.get(a.field as string);
    if (!el?.isConnected || el.disabled || el.readOnly) throw new Error('Text field changed. Tap the field again.');
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
