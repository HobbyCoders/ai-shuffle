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
 expect(screen.queryByRole('button')).toBeNull();
 expect(mocks.post).not.toHaveBeenCalled();
});
it('disabling updates preserves the saved scope but disables its toggle', async () => {
 mocks.get.mockResolvedValue({ allow_updates: false, update_all_workers: true });
 render(ApplicationUpdates);
 await screen.findByText(/Application updates are paused/);
 const toggle = screen.getByRole('switch', { name: 'Update all workers' });
 expect(toggle.hasAttribute('disabled')).toBe(true);
 expect((toggle as HTMLInputElement).checked).toBe(true);
});
