import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import NotificationsRow from './NotificationsRow.svelte';

const mocks = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), remove: vi.fn(), saved: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get, put: mocks.put, delete: mocks.remove }, getErrorMessage: () => 'Could not save credentials' }));
vi.mock('$lib/settings/registry', () => ({ getEntry: () => ({ label: 'Notifications' }) }));
vi.mock('$lib/settings/access.svelte', () => ({ getSettingsAccess: () => ({ readOnly: false }) }));
vi.mock('$lib/settings/navigation.svelte', () => ({ settingsNav: { pendingOpen: null, consumeOpen: () => false } }));
vi.mock('$lib/settings/save-status.svelte', () => ({ flashSaved: mocks.saved }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe('Needs you sender settings', () => {
    it('shows push off and opens a write-only credential field', async () => {
        mocks.get.mockResolvedValue({ configured: false, devices_registered: 0 });
        render(NotificationsRow);
        await screen.findByText('Push off · 0 devices');
        await fireEvent.click(screen.getByRole('button', { name: /Set up push/ }));
        const field = screen.getByLabelText('Firebase sender service-account JSON') as HTMLInputElement;
        expect(field.type).toBe('password');
        expect(field.value).toBe('');
        expect((screen.getByRole('button', { name: 'Save credentials' }) as HTMLButtonElement).disabled).toBe(true);
        expect(screen.getByText(/No completion or parking alerts/)).toBeTruthy();
    });
    it('clears pasted credentials after dispatch even if saving fails', async () => {
        mocks.get.mockResolvedValue({ configured: false, devices_registered: 2 });
        mocks.put.mockRejectedValue(new Error('not echoed'));
        render(NotificationsRow);
        await screen.findByText('Push off · 2 devices');
        await fireEvent.click(screen.getByRole('button', { name: /Set up push/ }));
        const field = screen.getByLabelText('Firebase sender service-account JSON') as HTMLInputElement;
        await fireEvent.input(field, { target: { value: 'private-json-input' } });
        await fireEvent.click(screen.getByRole('button', { name: 'Save credentials' }));
        await waitFor(() => expect(field.value).toBe(''));
        expect(mocks.put).toHaveBeenCalledWith('/settings/notifications', { service_account: 'private-json-input' });
        expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Could not save credentials');
        expect(document.body.textContent).not.toContain('private-json-input');
    });
    it('shows configured status without reading the key and supports removal', async () => {
        mocks.get.mockResolvedValue({ configured: true, devices_registered: 1 });
        mocks.remove.mockResolvedValue({ configured: false, devices_registered: 1 });
        render(NotificationsRow);
        await screen.findByText('Credentials set · 1 device');
        await fireEvent.click(screen.getByRole('button', { name: /Manage push/ }));
        await fireEvent.click(screen.getByRole('button', { name: 'Remove credentials' }));
        await screen.findByText('Push off · 1 device');
        expect(mocks.remove).toHaveBeenCalledWith('/settings/notifications');
    });
});
