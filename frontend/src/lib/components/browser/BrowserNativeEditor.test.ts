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
});
