import { readFileSync } from 'node:fs';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';
import type { ComposerSheetProps } from '../composer-shell';
import { EFFORT_OPTIONS, NO_ADJUSTABLE_EFFORT, UNKNOWN_EFFORT } from '$lib/constants/model-effort';
import MobileComposerControlsSheet from './MobileComposerControlsSheet.svelte';
import '$lib/test-stubs/composer-dom';

// Exercise the actual portaled sheet; unrelated pages/menus do not produce
// model capabilities. Preference reconciliation is exercised by the phone
// journey, not these mocks; the original CI transition's producer is unproven.
vi.mock('$lib/stores/active-session', () => ({ activeSession: writable({ sessionId: null, messages: [], activeTasks: [] }) }));
vi.mock('$lib/stores/watches', () => ({ watches: { ...writable({ list: [], preferredWorkerId: null }), track: () => () => {} } }));
vi.mock('$app/navigation', () => ({ pushState: () => { throw new Error('No router in this component test'); } }));
vi.mock('$lib/components/chat/ContextPillBar.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/chat/ContextUsageBar.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/chat/RunningTasksList.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/layout/ChatExportActions.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/settings/ModelPickerPanel.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));

afterEach(cleanup);
const adjustable = EFFORT_OPTIONS.filter(o => ['low', 'medium', 'high'].includes(o.value));
function props(overrides: Partial<ComposerSheetProps> = {}): ComposerSheetProps {
 return {
  open: true, onClose: vi.fn(), modelOptions: [], modelValue: 'sonnet', modelLabel: 'Sonnet', onModelSelect: vi.fn(),
  contextChoice: 'standard', onContextChoiceSelect: vi.fn(), effortOptions: adjustable, effortValue: null,
  effortKnown: true, effortDefaultValue: 'high', onEffortSelect: vi.fn(),
  permissionOptions: [{ value: 'default', label: 'Ask', description: 'Ask first' }],
  permissionValue: 'default', onPermissionSelect: vi.fn(), ...overrides
 };
}
const effortRow = () => document.querySelector<HTMLElement>('.mcp [data-row="effort"]');
const groupLabels = () => [...document.querySelectorAll('.mcp-glabel')];
const noteSlots = () => [...document.querySelectorAll<HTMLElement>('.mcp-effort-note')];
const currentNote = () => document.querySelector<HTMLElement>('.mcp-effort-note.current');

describe('phone effort slot across late capability changes', () => {
 it('reserves the intrinsic height of all explanations, not just the visible state', () => {
  // Vitest omits Svelte CSS and jsdom cannot measure positions. Guard the
  // sizing declarations as well as the rendered DOM below; the phone journey
  // measures following-group coordinates in a real browser.
  const source = readFileSync('src/lib/components/chat/phone/MobileComposerControlsSheet.svelte', 'utf8');
  expect(source).toMatch(/\.mcp-effort-body\s*\{[^}]*display:\s*grid/);
  expect(source).toMatch(/\.mcp-effort-body\s*>\s*\*\s*\{[^}]*grid-area:\s*1\s*\/\s*1/);
  expect(source).toMatch(/\.mcp-effort-note\s*\{[^}]*visibility:\s*hidden/);
  expect(source).toMatch(/\.mcp-effort-note\.current\s*\{[^}]*visibility:\s*visible/);
 });
 it.each([
  { effortKnown: false, effortExplanation: UNKNOWN_EFFORT, note: UNKNOWN_EFFORT },
  { effortKnown: true, effortExplanation: NO_ADJUSTABLE_EFFORT, note: NO_ADJUSTABLE_EFFORT }
 ])('keeps the row and intrinsic sizing slots when levels become $effortKnown', async ({ note, ...capability }) => {
  const initial = props();
  const view = render(MobileComposerControlsSheet, initial);
  const row = effortRow();
  const headings = groupLabels();
  const slots = noteSlots();
  expect(screen.getByRole('radiogroup', { name: 'Reasoning effort' })).toBeTruthy();
  await view.rerender({ ...initial, effortOptions: [], ...capability });
  expect(effortRow()).toBe(row);
  expect(effortRow()).not.toBeNull();
  expect(noteSlots()).toEqual(slots);
  expect(slots.length).toBeGreaterThan(0);
  for (const slot of slots) {
   expect(slot.getAttribute('aria-hidden')).toBe(slot === currentNote() ? 'false' : 'true');
  }
  expect(groupLabels()).toEqual(headings);
  expect(currentNote()?.textContent?.trim()).toBe(note);
  expect(currentNote()?.getAttribute('aria-hidden')).toBe('false');
  expect(screen.queryByRole('radiogroup', { name: 'Reasoning effort' })).toBeNull();
  expect(initial.onEffortSelect).not.toHaveBeenCalled();
 });

 it('reserves the same slot when unknown levels become adjustable, with live default and picks', async () => {
  const initial = props({ effortKnown: false, effortOptions: [], effortExplanation: UNKNOWN_EFFORT });
  const view = render(MobileComposerControlsSheet, initial);
  const row = effortRow();
  const slots = noteSlots();
  const headings = groupLabels();
  expect(row).not.toBeNull();
  expect(currentNote()?.textContent?.trim()).toBe(UNKNOWN_EFFORT);
  await view.rerender({ ...initial, effortKnown: true, effortOptions: adjustable, effortExplanation: undefined });
  expect(effortRow()).toBe(row);
  expect(noteSlots()).toEqual(slots);
  expect(groupLabels()).toEqual(headings);
  expect(currentNote()).toBeNull();
  const high = screen.getByRole('radio', { name: 'High' });
  expect(high.getAttribute('aria-describedby')).toBeTruthy();
  await fireEvent.click(high);
  expect(initial.onEffortSelect).toHaveBeenCalledWith('high');
 });

 it('opens after the capability flip with its final explanation, not stale levels or an empty gap', async () => {
  const initial = props({ open: false });
  const view = render(MobileComposerControlsSheet, initial);
  await view.rerender({ ...initial, effortKnown: false, effortOptions: [], effortExplanation: UNKNOWN_EFFORT });
  await view.rerender({ ...initial, open: true, effortKnown: false, effortOptions: [], effortExplanation: UNKNOWN_EFFORT });
  expect(effortRow()).not.toBeNull();
  expect(currentNote()?.textContent?.trim()).toBe(UNKNOWN_EFFORT);
  expect(screen.queryByRole('radiogroup', { name: 'Reasoning effort' })).toBeNull();
 });

 it('keeps a pinned unknown value visible but never offers unreported levels', () => {
  render(MobileComposerControlsSheet, props({ effortKnown: false, effortOptions: [], effortValue: 'ultra', effortExplanation: UNKNOWN_EFFORT }));
  expect(effortRow()?.querySelector('.mcp-val')?.textContent).toBe('ultra');
  expect(currentNote()?.textContent?.trim()).toBe(UNKNOWN_EFFORT);
  expect(screen.queryByRole('radio', { name: 'Ultra' })).toBeNull();
 });
});

describe('phone controls panel keeps keyboard focus while open (#1407)', () => {
 const panel = () => document.querySelector<HTMLElement>('.mcp')!;
 const tab = (shiftKey = false) => {
  const event = new KeyboardEvent('keydown', { key: 'Tab', shiftKey, bubbles: true, cancelable: true });
  (document.activeElement as HTMLElement).dispatchEvent(event);
  return event.defaultPrevented;
 };
 const tabbable = () => [...panel().querySelectorAll<HTMLElement>('button:not([disabled]), [role="radio"], input:not([disabled])')]
  .filter(el => el.getAttribute('tabindex') !== '-1' && !el.closest('[inert], [hidden]'));

 it('wraps Shift+Tab from the first control to the last, and Tab from the last to the first', () => {
  render(MobileComposerControlsSheet, props());
  const els = tabbable();
  expect(els.length).toBeGreaterThan(1);
  els[0].focus();
  expect(tab(true)).toBe(true);
  expect(panel().contains(document.activeElement)).toBe(true);
  expect(document.activeElement).not.toBe(els[0]);
  const last = document.activeElement as HTMLElement;
  expect(tab()).toBe(true);
  expect(document.activeElement).toBe(els[0]);
  // Between controls the browser moves focus as usual.
  last.focus();
  els[0].focus();
  expect(tab()).toBe(false);
 });

 it('does not hold Tab while closed', () => {
  render(MobileComposerControlsSheet, props({ open: false }));
  const close = panel().querySelector<HTMLElement>('.mcp-close')!;
  const event = new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true });
  close.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(false);
 });
});
