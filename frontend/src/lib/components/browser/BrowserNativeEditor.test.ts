import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import BrowserNativeEditor from './BrowserNativeEditor.svelte';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
function pointer(fine: boolean) {
 vi.stubGlobal('matchMedia', (query: string) => ({ matches: query === '(pointer: fine)' && fine, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
}
const box = (el: HTMLElement) => ['left', 'top', 'width', 'height'].map((k) => Math.round(parseFloat(el.style.getPropertyValue(k)) * 10) / 10);
const base = { scale: 1, left: 0, top: 0, viewWidth: 390, viewHeight: 700, onEdit: vi.fn(), onActive: vi.fn(), onEnter: vi.fn() };
describe('native remote text fields', () => {
 it('keeps existing text available to native selection and forwards replacement once', async () => {
  const onEdit = vi.fn(), onActive = vi.fn(), onEnter = vi.fn();
  render(BrowserNativeEditor, { field: { id: 'field', type: 'text', label: 'Message', value: 'existing text', x: 10, y: 20, width: 200, height: 40 }, scale: 1, left: 0, top: 0, viewWidth: 390, viewHeight: 700, onEdit, onActive, onEnter });
  await tick();
  const input = screen.getByRole('textbox') as HTMLInputElement;
  expect(input.value).toBe('existing text');
  input.focus();
  expect(onActive).toHaveBeenCalledWith(true);
  expect(input.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }))).toBe(true);
  await fireEvent.input(input, { target: { value: 'pasted text', selectionStart: 6, selectionEnd: 6 } });
  expect(onEdit).toHaveBeenLastCalledWith({ field: 'field', action: 'replace', value: 'pasted text', start: 6, end: 6 });
  await fireEvent.keyDown(input, { key: 'Enter' });
  expect(onEnter).toHaveBeenCalledOnce();
  input.blur();
  expect(onActive).toHaveBeenLastCalledWith(false);
 });
 it('sends split one-time-code digits as keystrokes so the page can advance focus', async () => {
  const onEdit = vi.fn(), onActive = vi.fn(), onEnter = vi.fn(), onText = vi.fn(), onKey = vi.fn();
  const field = { id: 'otp-1', type: 'tel', label: 'Digit 1', value: '', keys: true, x: 10, y: 20, width: 30, height: 40 };
  const view = render(BrowserNativeEditor, { field, scale: 1, left: 0, top: 0, viewWidth: 390, viewHeight: 700, onEdit, onActive, onEnter, onText, onKey });
  await tick();
  const input = screen.getByRole('textbox') as HTMLInputElement;
  input.focus();
  expect(onEdit).toHaveBeenCalledWith({ field: 'otp-1', action: 'focus' });
  for (const digit of ['1', '2', '3', '4', '5', '6']) {
   await fireEvent.input(input, { target: { value: digit } });
   // Nothing stays in the local box, so the next digit is never appended to an old one.
   expect(input.value).toBe('');
  }
  expect(onText.mock.calls.map(([text]) => text).join('')).toBe('123456');
  expect(onEdit).not.toHaveBeenCalledWith(expect.objectContaining({ action: 'replace' }));
  // The page moving its focus or rewriting the box never clears what was typed.
  await view.rerender({ field: { ...field, value: '1' }, scale: 1, left: 0, top: 0, viewWidth: 390, viewHeight: 700, onEdit, onActive, onEnter, onText, onKey });
  expect(input.value).toBe('');
  await fireEvent.keyDown(input, { key: 'Backspace' });
  expect(onKey).toHaveBeenCalledWith('Backspace');
  expect(input.classList.contains('focused')).toBe(false);
 });
 it('keeps replacement editing for ordinary fields even when keystroke callbacks exist', async () => {
  const onEdit = vi.fn(), onText = vi.fn(), onKey = vi.fn();
  render(BrowserNativeEditor, { field: { id: 'f', type: 'text', label: 'Name', value: '', x: 0, y: 0, width: 100, height: 30 }, scale: 1, left: 0, top: 0, viewWidth: 390, viewHeight: 700, onEdit, onActive: vi.fn(), onEnter: vi.fn(), onText, onKey });
  await tick();
  const input = screen.getByRole('textbox') as HTMLInputElement;
  input.focus();
  await fireEvent.input(input, { target: { value: 'Ada', selectionStart: 3, selectionEnd: 3 } });
  expect(onEdit).toHaveBeenLastCalledWith({ field: 'f', action: 'replace', value: 'Ada', start: 3, end: 3 });
  expect(onText).not.toHaveBeenCalled();
 });
});
describe('focused overlay geometry', () => {
 it.each(['text', 'textarea'])('keeps the page field box and stays see-through on a fine pointer (%s)', async (type) => {
  pointer(true);
  render(BrowserNativeEditor, { ...base, field: { id: 'f', type, label: 'Notes', value: 'hello', x: 10, y: 20, width: 120, height: 22 } });
  await tick();
  const input = screen.getByRole('textbox') as HTMLInputElement;
  input.focus();
  await tick();
  // No enlargement, no clamping into the view: nothing next to the field is covered.
  expect(box(input)).toEqual([10, 20, 120, 22]);
  expect(input.classList.contains('focused')).toBe(true);
  expect(input.classList.contains('enlarged')).toBe(false);
 });
 it('still enlarges a readable editor for touch thumbnails', async () => {
  pointer(false);
  render(BrowserNativeEditor, { ...base, scale: 0.3, surface: () => ({ background: 'rgb(255 255 255)', color: '#141414' }), field: { id: 'f', type: 'textarea', label: 'Notes', value: '', x: 10, y: 20, width: 120, height: 22 } });
  await tick();
  const input = screen.getByRole('textbox') as HTMLTextAreaElement;
  expect(box(input)).toEqual([3, 6, 36, 6.6]);
  input.focus();
  await tick();
  expect(box(input)).toEqual([8, 8, 240, 80]);
  expect(input.classList.contains('enlarged')).toBe(true);
  expect(input.style.getPropertyValue('--field-bg')).toBe('rgb(255 255 255)');
  input.blur();
  await tick();
  expect(input.classList.contains('enlarged')).toBe(false);
 });
});
describe('in-place editing on a fine pointer', () => {
 it('shows its own text in the page field colours, sized from the field', async () => {
  pointer(true);
  const surface = vi.fn(() => ({ background: 'rgb(255 255 255)', color: '#141414' }));
  render(BrowserNativeEditor, { ...base, scale: 0.7, surface, field: { id: 'f', type: 'text', label: 'Name', value: 'hello world', x: 10, y: 20, width: 200, height: 21 } });
  await tick();
  const input = screen.getByRole('textbox') as HTMLInputElement;
  expect(input.classList.contains('in-place')).toBe(false);
  // Idle uses the same type size, so the focusing click places the caret right.
  expect(parseFloat(input.style.fontSize)).toBeCloseTo(9.1, 1);
  expect(input.style.getPropertyValue('--field-bg')).toBe('');
  input.focus();
  await tick();
  expect(surface).toHaveBeenCalledOnce();
  expect(input.classList.contains('in-place')).toBe(true);
  expect(input.style.getPropertyValue('--field-bg')).toBe('rgb(255 255 255)');
  expect(input.style.getPropertyValue('--field-fg')).toBe('#141414');
  // 21px field → ~13px page text, at 0.7 scale.
  expect(parseFloat(input.style.fontSize)).toBeCloseTo(9.1, 1);
  expect(box(input)).toEqual([7, 14, 140, 14.7]);
 });
 it('falls back to theme colours when the frame cannot be read', async () => {
  pointer(true);
  render(BrowserNativeEditor, { ...base, surface: () => null, field: { id: 'f', type: 'textarea', label: 'Notes', value: '', x: 0, y: 0, width: 200, height: 80 } });
  await tick();
  const input = screen.getByRole('textbox') as HTMLTextAreaElement;
  input.focus();
  await tick();
  expect(input.classList.contains('in-place')).toBe(true);
  expect(input.style.getPropertyValue('--field-bg')).toBe('');
 });
 it('lands a mid-text edit at the clicked offset', async () => {
  pointer(true);
  const onEdit = vi.fn();
  render(BrowserNativeEditor, { ...base, onEdit, field: { id: 'f', type: 'text', label: 'Name', value: 'hello world', x: 0, y: 0, width: 200, height: 21 } });
  await tick();
  const input = screen.getByRole('textbox') as HTMLInputElement;
  input.focus();
  // The click put the visible caret after "hello"; typing inserts there.
  input.setSelectionRange(5, 5);
  input.setRangeText('X', 5, 5, 'end');
  await fireEvent.input(input);
  expect(input.value).toBe('helloX world');
  expect(onEdit).toHaveBeenLastCalledWith({ field: 'f', action: 'replace', value: 'helloX world', start: 6, end: 6 });
 });
});
describe('reported field metrics', () => {
 it('uses the page field font, padding and line height, scaled to the view', async () => {
  pointer(true);
  // 40px input, 12px padding, 14px text; a 16px/1.5 textarea.
  render(BrowserNativeEditor, { ...base, scale: 0.5, field: { id: 'f', type: 'text', label: 'Search', value: 'abc', x: 0, y: 0, width: 300, height: 40,
   style: { fontSize: 14, lineHeight: null, paddingLeft: 12, paddingTop: 10, fontFamily: '"Segoe UI", Roboto, sans-serif' } } });
  render(BrowserNativeEditor, { ...base, scale: 0.5, field: { id: 't', type: 'textarea', label: 'Notes', value: '', x: 0, y: 60, width: 300, height: 120,
   style: { fontSize: 16, lineHeight: 24, paddingLeft: 8, paddingTop: 6, fontFamily: 'x;background:url(https://evil.example)' } } });
  await tick();
  const input = screen.getByRole('textbox', { name: 'Search' }) as HTMLInputElement;
  const area = screen.getByRole('textbox', { name: 'Notes' }) as HTMLTextAreaElement;
  // Text starts at (1px page border + padding) × scale, inside the overlay's own 1px border.
  expect([input.style.fontSize, input.style.paddingLeft, input.style.paddingTop, input.style.lineHeight]).toEqual(['7px', '5.5px', '0px', 'normal']);
  expect(input.style.fontFamily).toContain('Segoe UI');
  expect([area.style.fontSize, area.style.paddingLeft, area.style.paddingTop, area.style.lineHeight]).toEqual(['8px', '3.5px', '2.5px', '12px']);
  // A family that would smuggle in another declaration is dropped.
  expect(area.style.fontFamily).toBe('');
  expect(area.style.background).toBe('');
  // Same metrics before and after focus: the text does not jump.
  // (Reported border widths are covered in the next test.)
  input.focus();
  await tick();
  expect([input.style.fontSize, input.style.paddingLeft]).toEqual(['7px', '5.5px']);
 });
});
describe('reported border widths', () => {
 it('starts the text after the page border when the runtime reports it', async () => {
  pointer(true);
  const style = { fontSize: 16, lineHeight: 24, paddingLeft: 8, paddingTop: 6, fontFamily: 'Georgia, serif' };
  render(BrowserNativeEditor, { ...base, scale: 0.5, field: { id: 't', type: 'textarea', label: 'Thick', value: '', x: 0, y: 0, width: 300, height: 120, style: { ...style, borderLeft: 3, borderTop: 5 } } });
  render(BrowserNativeEditor, { ...base, scale: 0.5, field: { id: 'z', type: 'text', label: 'Borderless', value: '', x: 0, y: 200, width: 300, height: 40, style: { ...style, paddingLeft: 0.5, borderLeft: 0, borderTop: 0 } } });
  await tick();
  const thick = screen.getByRole('textbox', { name: 'Thick' }) as HTMLTextAreaElement;
  const none = screen.getByRole('textbox', { name: 'Borderless' }) as HTMLInputElement;
  // (8 + 3) × 0.5 − 1 and (6 + 5) × 0.5 − 1, after the overlay's own 1px border.
  expect([thick.style.paddingLeft, thick.style.paddingTop]).toEqual(['4.5px', '4.5px']);
  // (0.5 + 0) × 0.5 − 1 would be negative: clamped at zero.
  expect(none.style.paddingLeft).toBe('0px');
 });
});
describe('navigation keys', () => {
 it('forwards arrows, paging, Tab and Escape to the page and leaves text keys local', async () => {
  pointer(true);
  const onNavKey = vi.fn();
  render(BrowserNativeEditor, { ...base, onNavKey, field: { id: 'f', type: 'text', label: 'City', value: '', x: 0, y: 0, width: 100, height: 30 } });
  await tick();
  const input = screen.getByRole('textbox') as HTMLInputElement;
  input.focus();
  // The local caret still moves, so the next replacement carries the same selection.
  expect(await fireEvent.keyDown(input, { key: 'ArrowDown', code: 'ArrowDown' })).toBe(true);
  expect(await fireEvent.keyDown(input, { key: 'PageUp', code: 'PageUp' })).toBe(true);
  expect(await fireEvent.keyDown(input, { key: 'ArrowLeft', code: 'ArrowLeft', shiftKey: true, ctrlKey: true })).toBe(true);
  // Tab moves the page's focus, never this app's.
  expect(await fireEvent.keyDown(input, { key: 'Tab', code: 'Tab', shiftKey: true })).toBe(false);
  await fireEvent.keyDown(input, { key: 'a', code: 'KeyA' });
  await fireEvent.keyDown(input, { key: 'ArrowUp', code: 'ArrowUp', isComposing: true });
  expect(onNavKey.mock.calls).toEqual([
   ['ArrowDown', 'ArrowDown', 40, 0],
   ['PageUp', 'PageUp', 33, 0],
   ['ArrowLeft', 'ArrowLeft', 37, 10],
   ['Tab', 'Tab', 9, 8],
  ]);
  await fireEvent.keyDown(input, { key: 'Escape', code: 'Escape' });
  expect(onNavKey).toHaveBeenLastCalledWith('Escape', 'Escape', 27, 0);
  expect(document.activeElement).not.toBe(input);
 });
});
