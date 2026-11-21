import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';
import ComposerContextLine from './ComposerContextLine.svelte';
import { activeSession } from '$lib/stores/active-session';
import { projects, sessions } from '$lib/stores/app-data.svelte';
vi.mock('$lib/stores/active-session', () => ({ activeSession: writable({}) }));
vi.mock('$lib/stores/app-data.svelte', () => ({ projects: writable([]), clusterProjects: writable([]), profiles: writable([{ id: 'agent', name: 'Agent' }]), sessions: writable([]) }));
vi.mock('$lib/stores/git.svelte', () => ({ gitStatus: writable(null) }));
vi.mock('$lib/stores/worker-presence', () => ({ workerInstalls: writable([{ is_self: true, device_name: 'Connected worker' }]), refreshWorkerPresence: vi.fn() }));
const session = activeSession as unknown as ReturnType<typeof writable<any>>;
const rows = sessions as unknown as ReturnType<typeof writable<any[]>>;
const projectRows = projects as unknown as ReturnType<typeof writable<any[]>>;
afterEach(cleanup);
beforeEach(() => {
 rows.set([]);
 projectRows.set([]);
 session.set({ sessionId: 'saved', profile: 'agent', ownerDeviceName: 'Owner worker', deviceIntent: { name: 'Stale intent' } });
});
describe('phone context ownership', () => {
 it('names the existing chat owner instead of the connected worker or draft intent', () => {
  render(ComposerContextLine, { newChat: false, sheetOpen: false, onOpen: vi.fn() });
  expect(screen.getByRole('group', { name: 'Chat context: Agent · Owner worker' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Runs on: Owner worker' })).toBeTruthy();
 });
 it('prefers the current server summary after an ownership move', () => {
  rows.set([{ id: 'saved', device_name: 'Moved owner' }]);
  render(ComposerContextLine, { newChat: false, sheetOpen: false, onOpen: vi.fn() });
  expect(screen.getByRole('button', { name: 'Runs on: Moved owner' })).toBeTruthy();
 });
 it('reserves device intent for an unsent chat', () => {
  session.set({ sessionId: null, profile: 'agent', deviceIntent: { name: 'Picked worker' } });
  render(ComposerContextLine, { newChat: true, sheetOpen: false, onOpen: vi.fn() });
  expect(screen.getByRole('button', { name: 'Runs on: Picked worker' })).toBeTruthy();
 });
});
describe('one button per name', () => {
 it('opens the agent and project selectors separately on a new chat', async () => {
  session.set({ sessionId: null, profile: 'agent', project: 'p1' });
  projectRows.set([{ id: 'p1', name: 'Dev sandbox' }]);
  const onOpen = vi.fn();
  render(ComposerContextLine, { newChat: true, sheetOpen: false, onOpen });
  await fireEvent.click(screen.getByRole('button', { name: 'Agent: Agent' }));
  expect(onOpen).toHaveBeenLastCalledWith('agent');
  await fireEvent.click(screen.getByRole('button', { name: 'Project: Dev sandbox' }));
  expect(onOpen).toHaveBeenLastCalledWith('project');
  expect(onOpen).toHaveBeenCalledTimes(2);
 });
 it('opens the project picker from "No project"', async () => {
  session.set({ sessionId: null, profile: 'agent' });
  const onOpen = vi.fn();
  render(ComposerContextLine, { newChat: true, sheetOpen: false, onOpen });
  await fireEvent.click(screen.getByRole('button', { name: 'Project: No project' }));
  expect(onOpen).toHaveBeenCalledWith('project');
 });
});
