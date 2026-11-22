import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { get } from 'svelte/store';
import type { SwarmSnapshot } from '$lib/utils/swarm';
const snapshot: SwarmSnapshot = {
 id: 'sw-1', session_id: 'session-swarm', title: 'Review the release', status: 'running',
 created_at: '2026-09-07T10:00:00Z', updated_at: '2026-09-07T10:01:00Z',
 tasks: [
  { id: 'code', title: 'Review code', agent: 'General Purpose', depends_on: [], status: 'running', worker_session_id: 'worker-1' },
  { id: 'tests', title: 'Review tests', agent: 'General Purpose', depends_on: [], status: 'completed', result: 'Tests cover the new route.' },
  { id: 'report', title: 'Write release report', agent: 'Writer', depends_on: ['code', 'tests'], status: 'queued' }
 ]
};

beforeEach(() => vi.resetModules());
afterEach(() => vi.unstubAllGlobals());
describe('swarm live store', () => {
 it('keeps an early live update over a stale authoritative response and deduplicates hydration', async () => {
  let complete!: (value: Response) => void;
  const fetcher = vi.fn((_url: string, _options?: RequestInit) => new Promise<Response>(resolve => complete = resolve));
  vi.stubGlobal('fetch', fetcher);
  const { swarms } = await import('./swarms');
  const request = swarms.hydrate(snapshot.session_id);
  const duplicate = swarms.hydrate(snapshot.session_id);
  expect(request).toBe(duplicate);
  const done = { ...snapshot, status: 'completed' as const, updated_at: '2026-09-07T10:02:00Z' };
  swarms.receive(done);
  complete(new Response(JSON.stringify({ swarms: [snapshot] }), { status: 200 }));
  await request;
  expect(get(swarms).bySession[snapshot.session_id][snapshot.id].status).toBe('completed');
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0][0]).toBe('/api/sessions/session-swarm/swarms');
 });
 it('rejects cross-session data and reports hydration failure with a retry', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response('', { status: 503 }))
   .mockResolvedValueOnce(new Response(JSON.stringify({ swarms: [snapshot] }), { status: 200 }));
  vi.stubGlobal('fetch', fetcher);
  const { swarms } = await import('./swarms');
  swarms.receive(snapshot, 'another-session');
  expect(get(swarms).bySession[snapshot.session_id]).toBeUndefined();
  await swarms.hydrate(snapshot.session_id);
  expect(get(swarms).errors[snapshot.session_id]).toContain('503');
  await swarms.hydrate(snapshot.session_id);
  expect(get(swarms).errors[snapshot.session_id]).toBe('');
 });
 it('applies confirmed cancellation without inventing a terminal result', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ swarm: { ...snapshot, status: 'cancelled' } }), { status: 200 })));
  const { swarms } = await import('./swarms');
  swarms.receive(snapshot);
  await swarms.cancel(snapshot.session_id, snapshot.id);
  expect(get(swarms).bySession[snapshot.session_id][snapshot.id].status).toBe('cancelled');
  expect(fetch).toHaveBeenCalledWith('/api/sessions/session-swarm/swarms/sw-1/cancel', expect.objectContaining({ method: 'POST' }));
 });
 it('treats a matching receipt as confirmed and refreshes the workflow', async () => {
  const receipt = { id: snapshot.id, session_id: snapshot.session_id, status: 'cancelled' };
  vi.stubGlobal('fetch', vi.fn()
   .mockResolvedValueOnce(new Response(JSON.stringify({ swarm: receipt }), { status: 200 }))
   .mockResolvedValueOnce(new Response(JSON.stringify({ swarms: [{ ...snapshot, status: 'cancelled' }] }), { status: 200 })));
  const { swarms } = await import('./swarms');
  await swarms.cancel(snapshot.session_id, snapshot.id);
  await vi.waitFor(() => expect(get(swarms).bySession[snapshot.session_id]?.[snapshot.id]?.status).toBe('cancelled'));
  expect(fetch).toHaveBeenLastCalledWith('/api/sessions/session-swarm/swarms', expect.anything());
 });
 it('rejects a receipt for another workflow', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(
   { swarm: { id: 'other', session_id: snapshot.session_id, status: 'cancelled' } }), { status: 200 })));
  const { swarms } = await import('./swarms');
  await expect(swarms.cancel(snapshot.session_id, snapshot.id)).rejects.toThrow('Cancellation was not confirmed');
 });
 it("says why a cancel was refused when the chat's worker explains", async () => {
  const detail = 'ServerWorker needs an update to cancel a swarm from here. Update it, or use Stop to stop everything in this chat.';
  vi.stubGlobal('fetch', vi.fn()
   .mockResolvedValueOnce(new Response(JSON.stringify({ detail }), { status: 409 }))
   .mockResolvedValueOnce(new Response('', { status: 404 })));
  const { swarms } = await import('./swarms');
  await expect(swarms.cancel(snapshot.session_id, snapshot.id)).rejects.toThrow(detail);
  await expect(swarms.cancel(snapshot.session_id, snapshot.id)).rejects.toThrow('Swarm request failed (404). Try again.');
 });
});
