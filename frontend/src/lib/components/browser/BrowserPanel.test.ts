import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import BrowserPanel from './BrowserPanel.svelte';
import type { BrowserStreamConfig } from '$lib/browser-panel/stream-client';
import type { BrowserWorkspaceState } from '$lib/browser-panel/workspace';

const mocks = vi.hoisted(() => ({
 config: null as BrowserStreamConfig | null,
 send: vi.fn((_payload: unknown) => true),
 panel: { open: true, expanded: false, width: 500, status: 'live', url: 'https://example.test', pageLoading: false, inspectMode: false, selectedElement: null },
}));
vi.mock('$lib/browser-panel/stream-client', () => ({ createBrowserStreamClient: (config: BrowserStreamConfig) => {
 mocks.config = config;
 return Object.freeze({ connect: () => {}, destroy: () => {}, send: mocks.send, isConnected: () => true });
} }));
vi.mock('$lib/stores/browser-panel.svelte', () => ({ browserPanel: {
 forSession: () => mocks.panel, current: { defaultViewport: { mode: 'fixed', width: 1280, height: 800 } },
 setStatus: vi.fn(), setPage: vi.fn(), resetPageTracking: vi.fn(), setSelectedElement: vi.fn(),
} }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: { subscribe: (callback: (value: null) => void) => { callback(null); return () => {}; } } }));
vi.mock('$lib/api/browser-panel', () => ({ startPanelBrowser: vi.fn(), uploadPanelFiles: vi.fn() }));

const state = (owner: 'agent' | 'human', pending = false): BrowserWorkspaceState => ({
 runtime_id: 'runtime', revision: 1, available: true, target_id: 'target', active_tab_id: 'tab',
 tabs: [{ id: 'tab', title: 'Example', url: 'https://example.test', current: true }],
 viewport: { mode: 'fixed', width: 1280, height: 800 },
 control: { owner, viewer_id: owner === 'human' ? 'viewer' : null, pending_viewer_id: pending ? 'viewer' : null }, busy: pending,
});
async function setup(workspace = state('human')) {
 const view = render(BrowserPanel, { sessionId: 'session' });
 await tick();
 mocks.config!.onOpen?.();
 mocks.config!.onHello?.('viewer');
 mocks.config!.onWorkspace?.(workspace);
 await tick();
 return view;
}
beforeEach(() => {
 vi.clearAllMocks();
 vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('browser control handoff', () => {
 it('releases without taking control and waits for authoritative ownership', async () => {
  const view = await setup();
  await fireEvent.click(view.getByRole('button', { name: 'Return control' }));
  expect(mocks.send).toHaveBeenCalledTimes(1);
  const command = mocks.send.mock.calls[0][0] as unknown as { id: string; op: string };
  expect(command).toMatchObject({ op: 'release' });
  mocks.config!.onCommandResult?.(command.id, true);
  await tick();
  expect(view.getByText('You control')).toBeTruthy();
  mocks.config!.onWorkspace?.(state('agent'));
  await tick();
  expect(view.getByText('Agent control')).toBeTruthy();
  expect(view.queryByRole('button', { name: 'Return control' })).toBeNull();
 });
 it('cancels pending control directly instead of waiting for takeover', async () => {
  const view = await setup(state('agent', true));
  await fireEvent.click(view.getByRole('button', { name: 'Return control' }));
  expect(mocks.send).toHaveBeenCalledTimes(1);
  expect(mocks.send.mock.calls[0][0]).toMatchObject({ op: 'release' });
 });
 it('keeps human ownership and displays a rejected release', async () => {
  const view = await setup();
  await fireEvent.click(view.getByRole('button', { name: 'Return control' }));
  const command = mocks.send.mock.calls[0][0] as { id: string };
  mocks.config!.onCommandResult?.(command.id, false, 'Browser command queue full');
  await waitFor(() => expect(view.getByText('Browser command queue full')).toBeTruthy());
  expect(view.getByText('You control')).toBeTruthy();
 });
 it('does not replay an awaiting viewer action after return control', async () => {
  const view = await setup(state('agent'));
  await fireEvent.click(view.getByLabelText('Browser settings'));
  await fireEvent.click(view.getByRole('button', { name: 'Rotate viewport' }));
  const takeover = mocks.send.mock.calls[0][0] as { id: string };
  expect(takeover).toMatchObject({ op: 'takeover' });
  mocks.config!.onWorkspace?.(state('agent', true));
  await tick();
  await fireEvent.click(view.getByRole('button', { name: 'Return control' }));
  const release = mocks.send.mock.calls[1][0] as { id: string };
  expect(release).toMatchObject({ op: 'release' });
  // A takeover already in flight may complete before the runtime receives release.
  mocks.config!.onWorkspace?.(state('human'));
  mocks.config!.onCommandResult?.(takeover.id, true);
  await waitFor(() => expect(view.getByText('Browser control was returned. Interact again to take control.')).toBeTruthy());
  expect(mocks.send).toHaveBeenCalledTimes(2);
  await fireEvent.click(view.getByRole('button', { name: 'Return control' }));
  expect(mocks.send).toHaveBeenCalledTimes(2);
  mocks.config!.onWorkspace?.(state('agent'));
  mocks.config!.onCommandResult?.(release.id, true);
  await tick();
  expect(view.getByText('Agent control')).toBeTruthy();
 });
});
