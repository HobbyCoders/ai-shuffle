import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import NotificationsRow from './NotificationsRow.svelte';

const mocks = vi.hoisted(() => ({ get: vi.fn(), go: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get }, getErrorMessage: () => 'Could not load status' }));
vi.mock('$lib/api/account', () => ({ ACCOUNT_CHANGED_EVENT: 'shuffle-account-changed' }));
vi.mock('$lib/settings/registry', () => ({ getEntry: () => ({ label: 'Notifications' }) }));
vi.mock('$lib/settings/access.svelte', () => ({ getSettingsAccess: () => ({ readOnly: false }) }));
vi.mock('$lib/settings/navigation.svelte', () => ({ settingsNav: { go: mocks.go, pendingOpen: null, consumeOpen: () => false } }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe('Phone notification relay status', () => {
    it('offers the existing account sign-in entry point without a key field', async () => {
        mocks.get.mockResolvedValue({ linked: false, devices: 0, enabled: false });
        render(NotificationsRow);
        await screen.findByText('Sign in to your HobbyCoders account to get phone notifications');
        await fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
        expect(mocks.go).toHaveBeenCalledWith({ category: 'devices', item: 'account' });
        expect(document.querySelector('input')).toBeNull();
    });
    it.each([0, 1, 2])('shows the linked worker’s device count (%s)', async (devices) => {
        mocks.get.mockResolvedValue({ linked: true, devices, enabled: true });
        render(NotificationsRow);
        await screen.findByText(`Phone notifications: on · ${devices} device${devices === 1 ? '' : 's'}`);
        expect(screen.queryByRole('button', { name: 'Sign in' })).toBeNull();
        expect(document.querySelector('input')).toBeNull();
    });
    it('refreshes after sign-in and sign-out, and removes its listener on teardown', async () => {
        mocks.get.mockResolvedValueOnce({ linked: false, devices: 0, enabled: false })
            .mockResolvedValueOnce({ linked: true, devices: 1, enabled: true });
        const view = render(NotificationsRow);
        await screen.findByRole('button', { name: 'Sign in' });
        window.dispatchEvent(new Event('shuffle-account-changed'));
        await screen.findByText('Phone notifications: on · 1 device');
        view.unmount();
        window.dispatchEvent(new Event('shuffle-account-changed'));
        await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
    });
    it('reports a failed status load without offering credential setup', async () => {
        mocks.get.mockRejectedValue(new Error('unavailable'));
        render(NotificationsRow);
        expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Could not load status');
        expect(screen.getByText('Phone notification status unavailable')).toBeTruthy();
        expect(document.querySelector('input')).toBeNull();
    });
});
