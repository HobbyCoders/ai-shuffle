import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import BrowserNativeEditor from './BrowserNativeEditor.svelte';
afterEach(cleanup);
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
