import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import TextField from './TextField.svelte';
afterEach(cleanup);
describe('TextField form keyboard paths', () => {
 it('uses an overridable desktop width so narrow rows can fill their control line', () => {
  render(TextField, { value: 'Before', label: 'Name', width: 260 });
  const field = screen.getByRole('textbox', { name: 'Name' }) as HTMLInputElement;
  expect(field.style.width).toBe('');
  expect(field.style.getPropertyValue('--st-text-width')).toBe('260px');
 });
 it('keeps default Enter/blur commits deduplicated', async () => {
  const onchange = vi.fn(); render(TextField, { value: 'Before', label: 'Name', onchange });
  const field = screen.getByRole('textbox', { name: 'Name' });
  await fireEvent.focus(field); await fireEvent.input(field, { target: { value: 'After' } });
  await fireEvent.keyDown(field, { key: 'Enter' }); await fireEvent.blur(field);
  expect(onchange).toHaveBeenCalledExactlyOnceWith('After');
 });
 it('lets an explicit-save form handle Enter and Escape without a second commit', async () => {
  const onchange = vi.fn(); const onkeydown = vi.fn((e: KeyboardEvent) => e.preventDefault());
  render(TextField, { value: 'Before', label: 'Name', onchange, onkeydown });
  const field = screen.getByRole('textbox', { name: 'Name' });
  await fireEvent.focus(field); await fireEvent.input(field, { target: { value: 'After' } });
  await fireEvent.keyDown(field, { key: 'Enter' }); await fireEvent.keyDown(field, { key: 'Escape' });
  expect(onkeydown).toHaveBeenCalledTimes(2); expect(onchange).not.toHaveBeenCalled();
 });
});
