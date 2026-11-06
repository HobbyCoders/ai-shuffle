import { cleanup, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import { createRawSnippet } from 'svelte';
import ActionButton from './ActionButton.svelte';

afterEach(cleanup);
describe('compact Settings actions', () => {
 it('opts into compact visuals without dropping native disabled semantics', () => {
  render(ActionButton, { compact: true, disabled: true, children: createRawSnippet(() => ({ render: () => '<span>Connect</span>' })) });
  const button = screen.getByRole('button', { name: 'Connect' }) as HTMLButtonElement;
  expect(button.classList.contains('compact')).toBe(true);
  expect(button.disabled).toBe(true);
 });
});
