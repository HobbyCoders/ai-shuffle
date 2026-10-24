import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import type { SwarmSnapshot } from '$lib/utils/swarm';

const rows = vi.hoisted(() => ({ store: null as null | import('svelte/store').Writable<Record<string, unknown>[]> }));
vi.mock('$lib/stores/app-data.svelte', async importOriginal => {
 const actual = await importOriginal<typeof import('$lib/stores/app-data.svelte')>();
 const { writable: w } = await import('svelte/store');
 rows.store = w<Record<string, unknown>[]>([]);
 return { ...actual, sessions: rows.store };
});

import SwarmCard from './SwarmCard.svelte';

const gaming = { name: 'GamingPC', device_id: 'dev_gaming' };
const snapshot = (id: string): SwarmSnapshot => ({
 id, session_id: `coordinator-${id}`, title: 'Cross-worker review', status: 'running',
 created_at: '2026-10-20T10:00:00Z', updated_at: '2026-10-20T10:01:00Z',
 tasks: [
  { id: 'remote', title: 'Review on the desktop', agent: 'general-purpose', depends_on: [], status: 'running', worker_session_id: 'w-1', worker: gaming },
  { id: 'local', title: 'Review beside the chat', agent: 'general-purpose', depends_on: [], status: 'queued' }
 ]
});
const taskRow = (title: string) => screen.getByText(title).closest('summary') as HTMLElement;
const show = (swarm: SwarmSnapshot) =>
 render(SwarmCard, { status: 'complete', expanded: true, sessionId: swarm.session_id, result: JSON.stringify({ swarm }) });

beforeEach(() => vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ swarms: [] }), { status: 200 })))));
afterEach(() => { cleanup(); rows.store?.set([]); vi.unstubAllGlobals(); });

describe('Swarm task worker', () => {
 it('names the worker on rows that run away from the chat, and only those', () => {
  const swarm = snapshot('elsewhere');
  rows.store!.set([{ id: swarm.session_id, owner_device_id: 'dev_unraid', device_name: 'Unraid' }]);
  show(swarm);
  const chip = within(taskRow('Review on the desktop')).getByTestId('swarm-task-worker');
  expect(chip.textContent).toBe('Runs on GamingPC');
  expect(chip.getAttribute('title')).toBe('Runs on GamingPC');
  expect(within(taskRow('Review beside the chat')).queryByTestId('swarm-task-worker')).toBeNull();
  // The expanded task names the worker in full (touch screens get no tooltip).
  expect(taskRow('Review on the desktop').parentElement!.querySelector('.runs-on')?.textContent).toBe('Runs on GamingPC · general-purpose');
  expect(taskRow('Review beside the chat').parentElement!.querySelector('.runs-on')).toBeNull();
 });

 it('stays quiet when the task runs on the chat’s own worker', () => {
  const swarm = snapshot('same');
  rows.store!.set([{ id: swarm.session_id, owner_device_id: 'dev_gaming', device_name: 'GamingPC' }]);
  show(swarm);
  expect(screen.queryByTestId('swarm-task-worker')).toBeNull();
 });

 it('still names a remote worker before the chat row has loaded', () => {
  show(snapshot('unknown'));
  expect(screen.getAllByTestId('swarm-task-worker').map(chip => chip.textContent)).toEqual(['Runs on GamingPC']);
 });
});
