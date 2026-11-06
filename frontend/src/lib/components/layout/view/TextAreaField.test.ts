import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import TextAreaField from './TextAreaField.svelte';

afterEach(cleanup);

describe('TextAreaField', () => {
 it('labels a multiline explicit-save input and reports every edit', async () => {
  const oninput = vi.fn();
  render(TextAreaField, { value: 'Draft', label: 'Instructions', rows: 6, oninput });
  const input = screen.getByRole('textbox', { name: 'Instructions' }) as HTMLTextAreaElement;
  expect(input.value).toBe('Draft');
  expect(input.rows).toBe(6);
  await fireEvent.input(input, { target: { value: 'Updated draft' } });
  expect(oninput).toHaveBeenCalledWith('Updated draft');
 });
});
