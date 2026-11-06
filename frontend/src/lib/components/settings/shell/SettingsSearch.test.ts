import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import SettingsSearch from './SettingsSearch.svelte';

afterEach(cleanup);
describe('Settings search recovery', () => {
 it('uses the shared empty state and Clear returns focus to the search', async () => {
  const { container } = render(SettingsSearch, { query: 'qzxqzxmissingword' });
  const input = screen.getByRole('combobox') as HTMLInputElement;
  expect(screen.getByRole('status').textContent).toContain('No setting matches');
  expect(container.querySelector('.st-empty-detail')).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: /^Clear$/ }));
  expect(input.value).toBe('');
  expect(document.activeElement).toBe(input);
  expect(screen.queryByRole('status')).toBeNull();
 });
});
