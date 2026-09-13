import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { swarms } from '$lib/stores/swarms';
import type { SwarmSnapshot } from '$lib/utils/swarm';
import ActiveSwarms from './ActiveSwarms.svelte';

let serial = 0;
function workflow(status: SwarmSnapshot['status'] = 'running'): SwarmSnapshot {
 const id = `active-workers-${++serial}`;
 return { id, session_id: id, title: 'Review release', status, created_at: '2026-09-13T10:00:00Z', updated_at: '2026-09-13T10:00:00Z', tasks: [{ id: 'review', title: 'Review code', agent: 'Reviewer', depends_on: [], status: 'running', worker_session_id: 'reviewer' }] };
}
function mockHydration(items: SwarmSnapshot[] = []) {
 vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ swarms: items }), { status: 200 })));
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it('hydrates without a mounted transcript tool, expands the real card, and removes terminal work', async () => {
 const swarm = workflow(); mockHydration([swarm]);
 render(ActiveSwarms, { sessionId: swarm.session_id });
 await waitFor(() => expect(screen.getByRole('button', { name: '1 worker running' })).toBeTruthy());
 expect(screen.queryByTestId('swarm-card')).toBeNull();
 await fireEvent.click(screen.getByRole('button', { name: '1 worker running' }));
 expect(screen.getByTestId('swarm-card')).toBeTruthy();
 expect(screen.getByText('Review code')).toBeTruthy();
 swarms.receive({ ...swarm, status: 'completed', updated_at: '2026-09-13T10:01:00Z' });
 await waitFor(() => expect(screen.queryByRole('region', { name: 'Active workers' })).toBeNull());
});
it.each(['completed', 'failed', 'cancelled', 'interrupted'] as const)('does not show %s workflows', async status => {
 const swarm = workflow(status); mockHydration(); swarms.receive(swarm);
 render(ActiveSwarms, { sessionId: swarm.session_id });
 expect(screen.queryByRole('region', { name: 'Active workers' })).toBeNull();
});
it('shows queued work and resets expansion when changing sessions', async () => {
 const swarm = workflow('queued'); swarm.tasks[0].status = 'queued'; mockHydration(); swarms.receive(swarm);
 const view = render(ActiveSwarms, { sessionId: swarm.session_id });
 await fireEvent.click(screen.getByRole('button', { name: 'Workers queued' }));
 expect(screen.getByTestId('swarm-card')).toBeTruthy();
 const other = workflow(); swarms.receive(other);
 await view.rerender({ sessionId: other.session_id });
 expect(screen.getByRole('button', { name: '1 worker running' }).getAttribute('aria-expanded')).toBe('false');
 expect(screen.queryByTestId('swarm-card')).toBeNull();
});
