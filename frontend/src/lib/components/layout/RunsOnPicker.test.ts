/**
 * #371 step 4: the agent card's "Runs on" menu. Keyboard users get the menu
 * pattern: focus lands on the picked row when it opens, the arrow keys and
 * Home/End move between rows, only the focused row is in the Tab order, and
 * Escape returns to the trigger.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
	setDeviceIntent: vi.fn(),
	listInstalls: vi.fn()
}));

vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	const state = writable({
		deviceIntent: null as { id: string; name: string } | null,
		worktreeIntent: 'local',
		project: null,
		projectV2Id: null
	});
	return { activeSession: { subscribe: state.subscribe, setDeviceIntent: mocks.setDeviceIntent } };
});
vi.mock('$lib/api/installs', () => ({
	listInstalls: mocks.listInstalls,
	getClusterGitState: vi.fn().mockResolvedValue(null)
}));

import RunsOnPicker from './RunsOnPicker.svelte';
import { resetWorkerPresence } from '$lib/stores/worker-presence';

const install = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
	id, device_name: name, base_url: `http://${id}`, platform: 'linux', version: '1', capabilities: {},
	last_seen: null, online: true, approved: true, ...extra
});

beforeEach(() => {
	resetWorkerPresence();
	mocks.setDeviceIntent.mockReset();
	mocks.listInstalls.mockResolvedValue({
		installs: [install('self', 'ExampleWorker', { is_self: true }), install('pc', 'Workstation'), install('laptop', 'Laptop')],
		self_instance_id: 'self'
	});
});
afterEach(() => {
	cleanup();
	resetWorkerPresence();
});

async function openMenu() {
	render(RunsOnPicker);
	const trigger = await screen.findByRole('button', { name: /runs on exampleworker/i });
	await fireEvent.click(trigger);
	await waitFor(() => expect(screen.getAllByRole('menuitemradio')).toHaveLength(3));
	return trigger;
}

const rows = () => screen.getAllByRole('menuitemradio') as HTMLButtonElement[];
const stops = () => rows().filter((el) => el.tabIndex === 0);

describe('RunsOnPicker keyboard', () => {
	it('focuses the picked row on open and keeps one row in the Tab order', async () => {
		await openMenu();
		await waitFor(() => expect(document.activeElement).toBe(rows()[0]));
		expect(rows()[0].getAttribute('aria-checked')).toBe('true');
		await waitFor(() => expect(stops()).toEqual([rows()[0]]));
	});

	it('moves with the arrow keys and Home/End, wrapping at the ends', async () => {
		await openMenu();
		await waitFor(() => expect(document.activeElement).toBe(rows()[0]));
		const menu = screen.getByRole('menu');

		await fireEvent.keyDown(menu, { key: 'ArrowDown' });
		expect(document.activeElement).toBe(rows()[1]);
		expect(stops()).toEqual([rows()[1]]);

		await fireEvent.keyDown(menu, { key: 'End' });
		expect(document.activeElement).toBe(rows()[2]);
		await fireEvent.keyDown(menu, { key: 'ArrowDown' });
		expect(document.activeElement).toBe(rows()[0]);
		await fireEvent.keyDown(menu, { key: 'ArrowUp' });
		expect(document.activeElement).toBe(rows()[2]);
		await fireEvent.keyDown(menu, { key: 'Home' });
		expect(document.activeElement).toBe(rows()[0]);
		// Moving is not picking: nothing was chosen yet.
		expect(mocks.setDeviceIntent).not.toHaveBeenCalled();
	});

	it('Escape closes the menu and returns focus to the trigger', async () => {
		const trigger = await openMenu();
		await fireEvent.keyDown(window, { key: 'Escape' });
		await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
		expect(document.activeElement).toBe(trigger);
	});

	it('floats the menu to <body> so the agent card cannot trap it under the composer', async () => {
		const trigger = await openMenu();
		const menu = screen.getByRole('menu');
		expect(menu.parentElement).toBe(document.body);
		expect(trigger.closest('.runs-on-wrap')?.contains(menu)).toBe(false);
		expect(menu.style.position).toBe('fixed');
		// A click inside the floated menu is not an outside click.
		await fireEvent.click(menu);
		expect(screen.queryByRole('menu')).not.toBeNull();
		await fireEvent.click(document.body);
		await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
	});

	it('Tab leaves the menu and closes it', async () => {
		await openMenu();
		await fireEvent.keyDown(screen.getByRole('menu'), { key: 'Tab' });
		await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
	});
});
