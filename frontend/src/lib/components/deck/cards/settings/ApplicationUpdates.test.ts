import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), desktop: null as any }));
vi.mock('$lib/api/client', () => ({ api: mocks, getErrorMessage: (cause: Error) => cause.message }));
vi.mock('$lib/services/desktop', () => ({ get desktop() { return mocks.desktop; } }));
import ApplicationUpdates from './ApplicationUpdates.svelte';
const state = { allow_updates: true, update_all_workers: false };
beforeEach(() => { vi.resetAllMocks(); mocks.desktop = null; mocks.get.mockResolvedValue(state); });
afterEach(cleanup);
it('shows enabled default and persists disabling without auto-downloading', async () => {
 mocks.put.mockResolvedValue({ ...state, allow_updates: false });
 render(ApplicationUpdates);
 const toggle = screen.getByRole('switch', { name: 'Allow updates' });
 await waitFor(() => expect(toggle.hasAttribute('disabled')).toBe(false));
 expect((toggle as HTMLInputElement).checked).toBe(true);
 await fireEvent.click(toggle);
 await waitFor(() => expect(mocks.put).toHaveBeenCalledWith('/settings/application-updates', { allow_updates: false }));
 expect(await screen.findByText(/Application updates are paused/)).toBeTruthy();
 expect(mocks.post).not.toHaveBeenCalled();
});
it('retains the saved policy and shows an error when saving fails', async () => {
 mocks.put.mockRejectedValue(new Error('Not allowed'));
 render(ApplicationUpdates);
 const toggle = screen.getByRole('switch', { name: 'Allow updates' });
 await waitFor(() => expect(toggle.hasAttribute('disabled')).toBe(false));
 await fireEvent.click(toggle);
 expect((await screen.findByRole('alert')).textContent).toContain('Not allowed');
 expect((toggle as HTMLInputElement).checked).toBe(true);
});
it('defaults all-workers off and persists only the scope setting', async () => {
 mocks.put.mockResolvedValue({ ...state, update_all_workers: true });
 render(ApplicationUpdates);
 const toggle = screen.getByRole('switch', { name: 'Update all workers' });
 await waitFor(() => expect(toggle.hasAttribute('disabled')).toBe(false));
 expect((toggle as HTMLInputElement).checked).toBe(false);
 await fireEvent.click(toggle);
 expect(mocks.put).toHaveBeenCalledWith('/settings/application-updates', { update_all_workers: true });
 await waitFor(() => expect((toggle as HTMLInputElement).checked).toBe(true));
 expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual(['Check for updates']);
 expect(mocks.post).not.toHaveBeenCalled();
});
it('disabling updates preserves the saved scope but disables its toggle', async () => {
 mocks.get.mockResolvedValue({ allow_updates: false, update_all_workers: true });
 render(ApplicationUpdates);
 await screen.findByText(/Application updates are paused/);
 const toggle = screen.getByRole('switch', { name: 'Update all workers' });
 expect(toggle.hasAttribute('disabled')).toBe(true);
 expect((toggle as HTMLInputElement).checked).toBe(true);
 expect(screen.getByRole('button', { name: 'Check for updates' }).hasAttribute('disabled')).toBe(true);
});

// #1623: Docker workers check on their own once a day, so a manual check is
// the only way to see a new build sooner.
const WORKERS = '/settings/application-updates/workers';
function serve(readings: object[][], policy = { ...state, update_all_workers: true }) {
 let reads = 0;
 mocks.get.mockImplementation(async (url: string) => url === WORKERS ? { workers: readings[Math.min(reads++, readings.length - 1)] } : policy);
}
const results = (...rows: string[]) => expect([...screen.getByRole('list', { name: 'Update check results' }).querySelectorAll('li')].map(li => li.textContent)).toEqual(rows);
it('checks every worker, waits for the checks to finish and shows each result', async () => {
 vi.useFakeTimers();
 const events = vi.fn();
 window.addEventListener('application-update-policy', events);
 try {
  serve([
   [{ device_id: 'local', phase: 'idle', request_pending: true }, { device_id: 'peer', phase: 'idle', request_pending: true }],
   [{ device_id: 'local', phase: 'checking' }, { device_id: 'peer', phase: 'idle' }],
   [{ device_id: 'local', phase: 'available', version: 'cccccccccccc', running_version: 'a'.repeat(40) },
    { device_id: 'peer', phase: 'idle', message: 'You are up to date.' }],
  ]);
  mocks.post.mockResolvedValue({ results: [
   { device_id: 'peer', device_name: 'Unraid', accepted: true, reason: 'Request queued' },
   { device_id: 'off', device_name: 'Laptop', accepted: false, reason: 'Offline; not queued' },
   { device_id: 'local', device_name: 'Current worker', accepted: true, reason: 'Request queued' }] });
  render(ApplicationUpdates);
  await vi.advanceTimersByTimeAsync(0);
  expect(screen.getByText(/Checks every online worker/)).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));
  expect(mocks.post).toHaveBeenCalledExactlyOnceWith('/settings/application-updates', { action: 'check' });
  const busy = screen.getByRole('button', { name: 'Checking…' });
  expect(busy.hasAttribute('disabled')).toBe(true);
  expect(busy.getAttribute('aria-busy')).toBe('true');
  // Pending, then checking, then two quiet readings.
  await vi.advanceTimersByTimeAsync(3 * 2000);
  expect(screen.queryByRole('list', { name: 'Update check results' })).toBeNull();
  await vi.advanceTimersByTimeAsync(2000);
  results('UnraidUp to date', 'LaptopOffline; not queued', 'Current workerUpdate available');
  expect(screen.getByRole('button', { name: 'Check for updates' }).hasAttribute('disabled')).toBe(false);
  // The sidebar button re-reads when the check is sent and when it finishes.
  expect(events).toHaveBeenCalledTimes(2);
 } finally { window.removeEventListener('application-update-policy', events); vi.useRealTimers(); }
});
it('stops waiting on a worker that never takes the check', async () => {
 vi.useFakeTimers();
 try {
  serve([[{ device_id: 'local', phase: 'idle', request_pending: true }]], state);
  mocks.post.mockResolvedValue({ results: [{ device_id: 'local', device_name: 'Current worker', accepted: true, reason: 'Request queued' }] });
  render(ApplicationUpdates);
  await vi.advanceTimersByTimeAsync(0);
  expect(screen.getByText(/Checks the worker you are connected to/)).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));
  await vi.advanceTimersByTimeAsync(92_000);
  results('Current workerStill checking');
 } finally { vi.useRealTimers(); }
});
it('reports a refused check and lets the owner try again', async () => {
 mocks.post.mockRejectedValue(new Error('Application updates are disabled in Account settings'));
 render(ApplicationUpdates);
 const button = await screen.findByRole('button', { name: 'Check for updates' });
 await waitFor(() => expect(button.hasAttribute('disabled')).toBe(false));
 await fireEvent.click(button);
 expect((await screen.findByRole('alert')).textContent).toContain('disabled in Account settings');
 expect(screen.getByRole('button', { name: 'Check for updates' }).hasAttribute('disabled')).toBe(false);
 expect(mocks.get).not.toHaveBeenCalledWith(WORKERS);
});
