import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it } from 'vitest';
import UpdateCheckResults from './UpdateCheckResults.svelte';

afterEach(cleanup);
it('shows each worker’s metadata without borrowing local identity or allowing remote refresh', async () => {
  render(UpdateCheckResults, { running: false, rows: [
    { id: 'self', name: 'Local worker', tone: 'ok', text: 'Up to date', runtime: {
      running_version: 'a'.repeat(40), updater: { revision: 'b'.repeat(40), capabilities: ['refresh-updater-v1'], refresh: 'available' }
    } },
    { id: 'peer', name: 'Other worker', tone: 'off', text: 'Offline', runtime: { updater: { revision: null, capabilities: [], refresh: 'unknown' }, diagnostics: null } }
  ] });
  await fireEvent.click(screen.getByRole('button', { name: 'Update details for Other worker' }));
  const other = screen.getByRole('region', { name: 'Other worker update details' });
  expect(other.textContent).not.toContain('aaaaaaaaaaaa');
  expect(other.textContent).not.toContain('bbbbbbbbbbbb');
  expect(other.textContent).toContain('Unknown');
  await fireEvent.click(screen.getByRole('button', { name: 'Update details for Local worker' }));
  expect(screen.getByText('bbbbbbbbbbbb')).toBeTruthy();
  expect(screen.queryByRole('button', { name: /Refresh updater|Refresh and restart/ })).toBeNull();
  expect(screen.queryByText('Restart the connected worker to refresh its updater?')).toBeNull();
});
