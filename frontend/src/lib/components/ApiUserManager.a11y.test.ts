/**
 * Each API user row repeats Edit / Rotate / Delete icon buttons; their
 * accessible names carry the user's name so assistive tech can tell rows apart.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';

const users = vi.hoisted(() =>
	['Alice', 'Bob'].map((name, i) => ({
		id: `u${i}`,
		name,
		username: null,
		description: null,
		project_id: null,
		profile_ids: null,
		is_active: true,
		model: null,
		effort: null,
		created_at: '2026-01-01T00:00:00Z',
		updated_at: '2026-01-01T00:00:00Z',
		last_used_at: null
	}))
);

vi.mock('$lib/api/apiUsers', () => ({
	getApiUsers: vi.fn(async () => users),
	createApiUser: vi.fn(),
	updateApiUser: vi.fn(),
	deleteApiUser: vi.fn(),
	rotateApiUserKey: vi.fn()
}));
vi.mock('$lib/api/client', () => ({ api: { get: vi.fn(async () => []) } }));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { readable } = await import('svelte/store');
	return { appData: { loadApiUserSessions: vi.fn() }, apiUserSessions: readable([]) };
});
vi.mock('$lib/stores/active-session', () => ({ activeSession: { switchSession: vi.fn() } }));
vi.mock('$lib/stores/layout.svelte', () => ({ layout: { openChat: vi.fn() } }));

import { render, screen, cleanup } from '@testing-library/svelte';
import ApiUserManager from './ApiUserManager.svelte';

afterEach(() => cleanup());

describe('ApiUserManager row actions', () => {
	it('names each row action with the user it acts on', async () => {
		render(ApiUserManager);
		await screen.findByText('Alice');

		for (const name of ['Alice', 'Bob']) {
			expect(screen.getByRole('button', { name: `Edit key ${name}` })).toBeTruthy();
			expect(screen.getByRole('button', { name: `Rotate API key for ${name}` })).toBeTruthy();
			expect(screen.getByRole('button', { name: `Delete key ${name}` })).toBeTruthy();
		}
		// No two row buttons share an accessible name.
		expect(screen.queryAllByRole('button', { name: /^(Edit|Rotate API key|Delete)$/ })).toHaveLength(0);
		// The visible tooltip is unchanged.
		expect(screen.getByRole('button', { name: 'Edit key Alice' }).getAttribute('title')).toBe('Edit');
	});
});
