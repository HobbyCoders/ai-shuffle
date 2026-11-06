import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import Checkbox from './Checkbox.svelte';
afterEach(cleanup);
describe('shared checkbox', () => {
 it('keeps a native labelled checkbox and reports its checked value', async () => {
  const onchange = vi.fn(); render(Checkbox, { label: 'Include diagnostics', onchange });
  const input = screen.getByRole('checkbox', { name: 'Include diagnostics' }) as HTMLInputElement;
  expect(input.checked).toBe(false); await fireEvent.click(input);
  expect(input.checked).toBe(true); expect(onchange).toHaveBeenLastCalledWith(true);
  await fireEvent.click(input); expect(onchange).toHaveBeenLastCalledWith(false);
 });
 it('exposes mixed and disabled states without losing the native control', () => {
  render(Checkbox, { label: 'All workers', checked: false, indeterminate: true, disabled: true });
  const input = screen.getByRole('checkbox', {name:'All workers'}) as HTMLInputElement;
  expect(input.indeterminate).toBe(true); expect(input.disabled).toBe(true);
 });
});
