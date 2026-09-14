import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { swarms } from '$lib/stores/swarms';
import type { SwarmSnapshot } from '$lib/utils/swarm';
import ActiveSwarms from './ActiveSwarms.svelte';
import ActiveSwarmsHarness from './ActiveSwarmsHarness.svelte';
import { writable } from 'svelte/store';
import { tick } from 'svelte';

let serial = 0;
function workflow(status: SwarmSnapshot['status'] = 'running'): SwarmSnapshot {
 const id = `active-workers-${++serial}`;
 return { id, session_id: id, title: 'Review release', status, created_at: '2026-09-13T10:00:00Z', updated_at: '2026-09-13T10:00:00Z', tasks: [{ id: 'review', title: 'Review code', agent: 'Reviewer', depends_on: [], status: 'running', worker_session_id: 'reviewer' }] };
}
function mockHydration(items: SwarmSnapshot[] = []) {
 vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ swarms: items }), { status: 200 })));
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
it('hydrates without a mounted transcript tool, expands the real card, and removes terminal work', async () => {
 const swarm = workflow(); mockHydration([swarm]);
 render(ActiveSwarms, { sessionId: swarm.session_id });
 await screen.findByRole('button', { name: 'Swarm Review release' });
 expect(screen.queryByRole('button', { name: /worker.*running|Workers queued/ })).toBeNull();
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
it('keeps workers and task details open when the parent session store receives messages', async () => {
 const swarm = workflow(); mockHydration([swarm]);
 const session = writable({ sessionId: swarm.session_id, messages: ['Start review'] });
 render(ActiveSwarmsHarness, { session });
 const toggle = await screen.findByRole('button', { name: 'Swarm Review release' });
 const details = screen.getByText('Review code').closest('details')!;
 details.open = true;
 await tick();
 const requests = vi.mocked(fetch).mock.calls.length;
 for (const message of ['New assistant message', 'Tool result received', 'Worker update']) {
  session.update(value => ({ ...value, messages: [...value.messages, message] }));
  await tick();
  expect(toggle.getAttribute('aria-expanded')).toBe('true');
  expect(screen.getByText('Review code').closest('details')).toBe(details);
  expect(details.open).toBe(true);
 }
 expect(vi.mocked(fetch).mock.calls.length).toBe(requests);
 swarms.receive({ ...swarm, updated_at: '2026-09-13T10:01:00Z' });
 await tick();
 expect(toggle.getAttribute('aria-expanded')).toBe('true');
 expect(details.open).toBe(true);
 const other = workflow(); swarms.receive(other);
 session.set({ sessionId: other.session_id, messages: ['Another session'] });
 await tick();
 expect(screen.getByRole('button', { name: 'Swarm Review release' }).getAttribute('aria-expanded')).toBe('true');
});
it('shows queued swarms directly and lets each card collapse independently', async () => {
 const first = workflow('queued'); first.tasks[0].status = 'queued';
 const second = { ...workflow(), session_id: first.session_id, title: 'Check tests' };
 mockHydration([first, second]);
 render(ActiveSwarms, { sessionId: first.session_id });
 const firstHeading = await screen.findByRole('button', { name: 'Swarm Review release' });
 const secondHeading = screen.getByRole('button', { name: 'Swarm Check tests' });
 expect(screen.getAllByTestId('swarm-card')).toHaveLength(2);
 expect(firstHeading.getAttribute('aria-expanded')).toBe('true');
 expect(secondHeading.getAttribute('aria-expanded')).toBe('true');
 await fireEvent.click(firstHeading);
 expect(firstHeading.getAttribute('aria-expanded')).toBe('false');
 expect(secondHeading.getAttribute('aria-expanded')).toBe('true');
 swarms.receive({ ...first, updated_at: '2026-09-13T10:01:00Z' });
 await tick();
 expect(firstHeading.getAttribute('aria-expanded')).toBe('false');
 await fireEvent.click(firstHeading);
 expect(firstHeading.getAttribute('aria-expanded')).toBe('true');
});
