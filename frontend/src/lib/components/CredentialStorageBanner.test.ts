import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import { tick } from 'svelte';
import type { AuthStatus } from '$lib/api/client';

const state = writable<{ status: Partial<AuthStatus> | null }>({ status: null });
vi.mock('$lib/stores/auth.svelte', () => ({ auth: { subscribe: (...args: Parameters<typeof state.subscribe>) => state.subscribe(...args) } }));
import CredentialStorageBanner from './CredentialStorageBanner.svelte';

afterEach(() => { cleanup(); state.set({ status: null }); });

describe('credential storage notice', () => {
	it('explains shared database recovery and disappears after recovery', async () => {
		state.set({ status: { is_admin: true, credential_storage: { ready: false, state: 'recovery_required' } } });
		render(CredentialStorageBanner);
		expect(screen.getByRole('alert').textContent).toContain('Shuffle install that initialized this database');
		state.set({ status: { is_admin: true, credential_storage: { ready: true, state: 'ready' } } });
		await tick();
		expect(screen.queryByRole('alert')).toBeNull();
	});
	it('distinguishes legacy adoption from missing custody', () => {
		state.set({ status: { is_admin: true, credential_storage: { ready: false, state: 'legacy_unlock_required' } } });
		render(CredentialStorageBanner);
		expect(screen.getByRole('alert').textContent).toContain('original encryption password');
	});
	it.each([null, { is_admin: true }, { is_admin: false, credential_storage: { ready: false, state: 'recovery_required' } }])(
		'hides recovery details when readiness or admin access is absent', (status) => {
			state.set({ status });
			render(CredentialStorageBanner);
			expect(screen.queryByRole('alert')).toBeNull();
		}
	);
});
