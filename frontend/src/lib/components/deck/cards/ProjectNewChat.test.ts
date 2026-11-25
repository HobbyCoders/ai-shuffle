/** "New chat" on the project page: the default worker, and the menu of every worker. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
vi.mock('$lib/stores/layout.svelte', () => ({ layout: { openChat: vi.fn() } }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: { newChat: vi.fn(), setProject: vi.fn(), setDeviceIntent: vi.fn() } }));
import ProjectNewChat, { defaultChatWorker } from './ProjectNewChat.svelte';
import { activeSession } from '$lib/stores/active-session';
import { layout } from '$lib/stores/layout.svelte';

const w = (install_id: string, device_name: string, state: string, online = true) =>
	({ install_id, device_name, is_self: install_id === 'self', online, approved: true, state, setup: null } as never);
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('ProjectNewChat', () => {
	it('prefers this worker, then the first online worker that has the project', () => {
		expect(defaultChatWorker([w('peer', 'Desktop', 'ready'), w('self', 'Home server', 'ready')])).toMatchObject({ install_id: 'self' });
		expect(defaultChatWorker([w('self', 'Home server', 'missing'), w('off', 'Laptop', 'offline_has', false), w('peer', 'Desktop', 'ready')])).toMatchObject({ install_id: 'peer' });
		expect(defaultChatWorker([w('self', 'Home server', 'missing')])).toBeNull();
	});

	it('starts on another worker when this one lacks the project', async () => {
		render(ProjectNewChat, { workers: [w('self', 'Home server', 'missing'), w('peer', 'Desktop', 'ready')], kind: 'git', localProjectId: null, projectV2Id: 'pv2' });
		await fireEvent.click(screen.getByRole('button', { name: 'New chat on Desktop' }));
		expect(activeSession.setProject).toHaveBeenCalledWith('', 'pv2', { user: true });
		expect(activeSession.setDeviceIntent).toHaveBeenCalledWith({ id: 'peer', name: 'Desktop' });
		expect(layout.openChat).toHaveBeenCalledWith(null);
	});

	it('lists every worker and only lets the ready, online ones start a chat', async () => {
		render(ProjectNewChat, { workers: [w('self', 'Home server', 'ready'), w('peer', 'Desktop', 'missing'), w('off', 'Laptop', 'offline_has', false)], kind: 'git', localProjectId: 'demo', projectV2Id: 'pv2' });
		expect(screen.getByRole('button', { name: 'New chat on Home server' })).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Choose a worker' }));
		const items = screen.getAllByRole('menuitem');
		expect(items.map((i) => i.getAttribute('aria-disabled') === 'true')).toEqual([false, true, true]);
	});

	it('moves keyboard focus into the menu even when no worker can start a chat', async () => {
		render(ProjectNewChat, { workers: [w('self', 'Home server', 'missing'), w('off', 'Laptop', 'offline_has', false)], kind: 'git', localProjectId: null, projectV2Id: 'pv2' });
		const caret = screen.getByRole('button', { name: 'Choose a worker' });
		caret.focus();
		await fireEvent.click(caret);
		const items = await screen.findAllByRole('menuitem');
		expect(items.every((i) => i.getAttribute('aria-disabled') === 'true')).toBe(true);
		await waitFor(() => expect(document.activeElement).toBe(items[0]));
		// Arrow keys still walk the greyed rows, which say why they are unavailable.
		await fireEvent.keyDown(items[0], { key: 'ArrowDown' });
		expect(document.activeElement).toBe(items[1]);
		expect(activeSession.newChat).not.toHaveBeenCalled();
	});

	it('is disabled until some worker has the project', () => {
		render(ProjectNewChat, { workers: [w('self', 'Home server', 'missing')], kind: 'git', localProjectId: null, projectV2Id: 'pv2' });
		expect((screen.getByRole('button', { name: 'New chat' }) as HTMLButtonElement).disabled).toBe(true);
	});
});
