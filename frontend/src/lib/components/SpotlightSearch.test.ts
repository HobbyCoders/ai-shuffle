import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
const mocks = vi.hoisted(() => ({ get: vi.fn(), sessions: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get } }));
vi.mock('$lib/api/sessions', () => ({ searchSessionsPaginated: mocks.sessions }));
import SpotlightSearch from './SpotlightSearch.svelte';

const props = () => ({ visible: true, sessions: [], onClose: vi.fn(), onSelectSession: vi.fn(),
 onSelectAgent: vi.fn(), onNewChat: vi.fn(), onOpenSettings: vi.fn(), onOpenFeature: vi.fn() });
async function search(query: string) {
 await fireEvent.input(screen.getByRole('textbox'), { target: { value: query } });
 await vi.advanceTimersByTimeAsync(260);
 await tick();
}
beforeEach(() => {
 vi.useFakeTimers();
 Element.prototype.scrollIntoView = vi.fn();
 mocks.get.mockReset().mockImplementation(async (path: string) => {
  if (path === '/agents') return [{ id: 'agent', name: 'Orchid agent' }];
  if (path === '/mcp/servers') return [{ name: 'Orchid MCP', scope: 'user', env: { SECRET: 'never-render' } }];
  if (path === '/plugins/installed') return [{ id: 'plugin', name: 'Orchid plugin' }];
  if (path.startsWith('/notes?')) return [{ id: 'note', title: 'Orchid note', content: 'Saved orchid details' }];
  if (path.startsWith('/documentation/')) return { results: [{ category: 'features', slug: 'orchid', title: 'Orchid guide', snippets: ['Orchid help'] }] };
  return [];
 });
 mocks.sessions.mockReset().mockResolvedValue({ items: [{ id: 'session', title: 'Unrelated title', match_type: 'content', match_snippet: 'Assistant discussed orchid' }] });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('universal search', () => {
 it('starts without hardcoded sessions or agents and keeps quick actions', () => {
  render(SpotlightSearch, { props: props() });
  expect(screen.queryByRole('region', { name: 'Sessions' })).toBeNull();
  expect(screen.queryByRole('region', { name: 'Agents' })).toBeNull();
  expect(screen.getByText('New conversation')).toBeTruthy();
  expect(mocks.get).not.toHaveBeenCalled();
 });
 it('groups only matching results and opens content-only sessions', async () => {
  const p = props(); render(SpotlightSearch, { props: p });
  await search('orchid');
  for (const name of ['Sessions', 'Agents', 'MCP servers', 'Plugins', 'Notes', 'Documentation'])
   expect(screen.getByRole('region', { name })).toBeTruthy();
  expect(screen.queryByRole('region', { name: 'Hooks' })).toBeNull();
  expect(screen.queryByText('never-render')).toBeNull();
  await fireEvent.click(screen.getByText('Unrelated title'));
  expect(p.onSelectSession).toHaveBeenCalledWith(expect.objectContaining({ id: 'session' }));
 });
 it('debounces and searches all sessions, not just the current project', async () => {
  render(SpotlightSearch, { props: { ...props(), currentProjectId: 'project' } });
  for (const value of ['o', 'or', 'orchid']) await fireEvent.input(screen.getByRole('textbox'), { target: { value } });
  await vi.advanceTimersByTimeAsync(260);
  expect(mocks.sessions).toHaveBeenCalledExactlyOnceWith('orchid', { limit: 20 });
 });
 it('keeps keyboard indices aligned across dynamic groups', async () => {
  const p = props(); render(SpotlightSearch, { props: p }); await search('orchid');
  await fireEvent.keyDown(window, { key: 'ArrowDown' });
  await fireEvent.keyDown(window, { key: 'Enter' });
  expect(p.onSelectAgent).toHaveBeenCalledWith('agent');
 });
 it('opens the selected note and doc, not just their list', async () => {
  const p = props(); render(SpotlightSearch, { props: p }); await search('orchid');
  await fireEvent.click(screen.getByText('Orchid note'));
  expect(p.onOpenFeature).toHaveBeenCalledWith('notes', 'note');
  await fireEvent.click(screen.getByText('Orchid guide'));
  expect(p.onOpenFeature).toHaveBeenCalledWith('docs', 'features/orchid');
 });
 it('does not let an old request repopulate a cleared query', async () => {
  let resolve!: (result: unknown) => void;
  mocks.sessions.mockReturnValueOnce(new Promise(r => { resolve = r; }));
  render(SpotlightSearch, { props: props() }); await search('orchid'); await search('');
  resolve({ items: [{ id: 'old', title: 'Stale result' }] }); await tick(); await vi.advanceTimersByTimeAsync(1);
  expect(screen.queryByText('Stale result')).toBeNull();
 });
 it('invalidates an old result during the next debounce window', async () => {
  let resolve!: (result: unknown) => void;
  mocks.sessions.mockReturnValueOnce(new Promise(r => { resolve = r; }));
  render(SpotlightSearch, { props: props() }); await search('old');
  await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'new' } });
  resolve({ items: [{ id: 'old', title: 'Stale result' }] }); await tick(); await vi.advanceTimersByTimeAsync(1);
  expect(screen.queryByText('Stale result')).toBeNull();
 });
 it('reports partial failures while keeping successful sources', async () => {
  mocks.sessions.mockRejectedValue(new Error('offline'));
  render(SpotlightSearch, { props: props() }); await search('orchid');
  expect(screen.getByText('Orchid note')).toBeTruthy();
  expect(screen.getByText('Could not search: Sessions. Try again.')).toBeTruthy();
 });
 it('refreshes configuration on reopen', async () => {
  const p = props(); const view = render(SpotlightSearch, { props: p }); await search('orchid');
  await search('other');
  expect(mocks.get.mock.calls.filter(([path]) => path === '/agents')).toHaveLength(1);
  await view.rerender({ ...p, visible: false }); await view.rerender(p); await search('orchid');
  expect(mocks.get.mock.calls.filter(([path]) => path === '/agents')).toHaveLength(2);
 });
});
