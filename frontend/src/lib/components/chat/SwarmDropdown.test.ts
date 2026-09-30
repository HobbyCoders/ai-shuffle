import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { swarms } from '$lib/stores/swarms';
import type { SwarmSnapshot } from '$lib/utils/swarm';
import SwarmDropdown from './SwarmDropdown.svelte';

function snapshot(id: string, session_id: string, status: 'running' | 'completed'): SwarmSnapshot {
	return { id, session_id, title: id, status,
		created_at: '2026-09-30T10:00:00Z', updated_at: '2026-09-30T10:00:01Z',
		tasks: [{ id: 'task', title: 'Worker task', agent: 'Fixture', depends_on: [], status,
			task_id: 'worker', worker_session_id: 'worker', result: null, error: null, started_at: null, finished_at: null }] };
}
function api(rows: SwarmSnapshot[]) {
	const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ swarms: rows }), { status: 200 }));
	vi.stubGlobal('fetch', fetch); return fetch;
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Swarm workflow dropdown', () => {
	it('keeps active and completed workflow history behind its disclosure', async () => {
		const sid = 'dropdown-history';
		api([snapshot('Running workflow', sid, 'running'), snapshot('Finished workflow', sid, 'completed')]);
		render(SwarmDropdown, { sessionId: sid });
		await waitFor(() => expect(screen.getByText('1 active')).toBeTruthy());
		const disclosure = screen.getByText('Swarm workflows').closest('details')!;
		expect(disclosure.open).toBe(false);
		await fireEvent.click(screen.getByText('Swarm workflows'));
		expect(disclosure.open).toBe(true);
		expect(screen.getByRole('button', { name: 'Swarm Running workflow' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Swarm Finished workflow' })).toBeTruthy();
	});
	it('provides a standalone dropdown when there is no Git menu', () => {
		api([]);
		const view = render(SwarmDropdown, { sessionId: 'dropdown-standalone', standalone: true });
		expect(view.container.querySelector('details.standalone')).not.toBeNull();
	});
	it('dismisses the standalone popup with Escape or an outside click', async () => {
		api([]);
		render(SwarmDropdown, { sessionId: 'dropdown-dismiss', standalone: true });
		const summary = screen.getByText('Swarm workflows');
		const disclosure = summary.closest('details')!;
		await fireEvent.click(summary);
		expect(disclosure.open).toBe(true);
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(disclosure.open).toBe(false);
		await fireEvent.click(summary);
		await fireEvent.click(document.body);
		expect(disclosure.open).toBe(false);
	});

	it('hydrates the session store without adding a duplicate header control', async () => {
		const sid = 'dropdown-hydrate';
		api([snapshot('Hydrated workflow', sid, 'completed')]);
		const view = render(SwarmDropdown, { sessionId: sid, hydrateOnly: true });
		await waitFor(() => expect(get(swarms).bySession[sid]?.['Hydrated workflow']).toBeTruthy());
		expect(view.container.querySelector('details')).toBeNull();
	});
});
