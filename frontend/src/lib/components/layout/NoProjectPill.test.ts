/**
 * #1663: the attach sheet belongs to the chat it was opened on. The pill stays
 * mounted when the page moves to another no-project chat, so that move must
 * close the sheet and drop its pick and error text.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
	attachProject: vi.fn(),
	session: null as unknown as import('svelte/store').Writable<Record<string, unknown>>,
}));

vi.mock('$lib/stores/active-session', async () => {
	const { writable, get } = await import('svelte/store');
	mocks.session = writable<Record<string, unknown>>({});
	return {
		activeSession: {
			subscribe: mocks.session.subscribe,
			get current() { return get(mocks.session); },
			applyAttachedProject: vi.fn(),
			waitForAttachedRow: vi.fn(),
		},
	};
});
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	return {
		profiles: writable([{ id: 'agent', name: 'Juniper' }]),
		sessions: writable([{ id: 'chat-a' }, { id: 'chat-b' }]),
		projects: writable([{ id: 'proj-local', name: 'ai-shuffle', path: '/w/ai-shuffle' }]),
		clusterProjects: writable([]),
	};
});
vi.mock('$lib/stores/archived-chats', async () => {
	const { writable } = await import('svelte/store');
	return { archivedChats: writable(new Set<string>()) };
});
vi.mock('$lib/stores/subagent-chats', async () => {
	const { writable } = await import('svelte/store');
	return { subagentChats: writable(new Set<string>()) };
});
vi.mock('$lib/stores/worker-presence', async () => {
	const { writable } = await import('svelte/store');
	return { watchWorkerPresence: () => () => {}, workerInstalls: writable([]) };
});
vi.mock('$lib/api/sessions', () => ({ attachProject: mocks.attachProject }));
vi.mock('$lib/stores/notifications', () => ({ notify: vi.fn() }));

import { tick } from 'svelte';
import NoProjectPill from './NoProjectPill.svelte';

const chat = (sessionId: string) => ({ sessionId, mode: 'chat', project: null, projectV2Id: null, profile: 'agent', isRemote: false });
const dialog = () => screen.queryByRole('dialog');
const pill = () => screen.getByRole('button', { name: /attach a project to this session/i });

beforeEach(() => {
	mocks.attachProject.mockReset();
	mocks.session.set(chat('chat-a'));
});
afterEach(() => cleanup());

describe('NoProjectPill', () => {
	it('closes the sheet, pick and all, when the page moves to another chat', async () => {
		render(NoProjectPill);
		await fireEvent.click(pill());
		expect(dialog()).toBeTruthy();
		await fireEvent.click(screen.getByRole('radio', { name: /ai-shuffle/ }));
		expect(screen.getByRole('radio', { name: /ai-shuffle/ }).getAttribute('aria-checked')).toBe('true');

		mocks.session.set(chat('chat-b'));
		await tick();
		expect(dialog()).toBeNull();
		expect(pill().getAttribute('aria-expanded')).toBe('false');

		// Opened on the new chat, it starts with nothing picked.
		await fireEvent.click(pill());
		expect(dialog()).toBeTruthy();
		expect(screen.getByRole('radio', { name: /ai-shuffle/ }).getAttribute('aria-checked')).toBe('false');
	});

	it('does not reopen when the page comes back to the chat it was opened on', async () => {
		render(NoProjectPill);
		await fireEvent.click(pill());
		mocks.session.set(chat('chat-b'));
		await tick();
		mocks.session.set(chat('chat-a'));
		await tick();
		expect(dialog()).toBeNull();
	});

	it('drops the old chat’s error text', async () => {
		mocks.attachProject.mockRejectedValue(new Error('That project is busy.'));
		render(NoProjectPill);
		await fireEvent.click(pill());
		await fireEvent.click(screen.getByRole('radio', { name: /ai-shuffle/ }));
		await fireEvent.click(screen.getByRole('button', { name: /^attach/i }));
		await screen.findByText(/That project is busy/);

		mocks.session.set(chat('chat-b'));
		await tick();
		await fireEvent.click(pill());
		expect(dialog()).toBeTruthy();
		expect(screen.queryByText(/That project is busy/)).toBeNull();
	});
});
