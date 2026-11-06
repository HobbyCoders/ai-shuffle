import { cleanup, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';
import ComposerContextLine from './ComposerContextLine.svelte';
import { activeSession } from '$lib/stores/active-session';
import { sessions } from '$lib/stores/app-data.svelte';
vi.mock('$lib/stores/active-session', () => ({ activeSession: writable({}) }));
vi.mock('$lib/stores/app-data.svelte', () => ({ projects: writable([]), clusterProjects: writable([]), profiles: writable([{ id: 'agent', name: 'Agent' }]), sessions: writable([]) }));
vi.mock('$lib/stores/git.svelte', () => ({ gitStatus: writable(null) }));
vi.mock('$lib/stores/worker-presence', () => ({ workerInstalls: writable([{ is_self: true, device_name: 'Connected worker' }]), refreshWorkerPresence: vi.fn() }));
const session = activeSession as unknown as ReturnType<typeof writable<any>>;
const rows = sessions as unknown as ReturnType<typeof writable<any[]>>;
afterEach(cleanup);
beforeEach(() => { rows.set([]); session.set({ sessionId: 'saved', profile: 'agent', ownerDeviceName: 'Owner worker', deviceIntent: { name: 'Stale intent' } }); });
describe('phone context ownership', () => {
 it('names the existing chat owner instead of the connected worker or draft intent', () => {
  render(ComposerContextLine, { newChat: false, sheetOpen: false, onOpen: vi.fn() });
  expect(screen.getByRole('button', { name: /Agent · Owner worker/ })).toBeTruthy();
 });
 it('prefers the current server summary after an ownership move', () => {
  rows.set([{ id: 'saved', device_name: 'Moved owner' }]);
  render(ComposerContextLine, { newChat: false, sheetOpen: false, onOpen: vi.fn() });
  expect(screen.getByRole('button', { name: /Agent · Moved owner/ })).toBeTruthy();
 });
 it('reserves device intent for an unsent chat', () => {
  session.set({ sessionId: null, profile: 'agent', deviceIntent: { name: 'Picked worker' } });
  render(ComposerContextLine, { newChat: true, sheetOpen: false, onOpen: vi.fn() });
  expect(screen.getByRole('button', { name: /Picked worker/ })).toBeTruthy();
 });
});
