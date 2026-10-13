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

describe('native keyboard in fit viewport', () => {
 it.each(['agent', 'human'] as const)('freezes remote resizing while focused and resyncs on blur (initial owner: %s)', async (owner) => {
  vi.useFakeTimers();
  const observers = new Map<Element, () => void>();
  vi.stubGlobal('ResizeObserver', class {
   target?: Element;
   constructor(private callback: () => void) {}
   observe(target: Element) { this.target = target; observers.set(target, this.callback); }
   disconnect() { if (this.target) observers.delete(this.target); }
  });
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
  vi.stubGlobal('createImageBitmap', vi.fn().mockResolvedValue({ close: vi.fn() }));
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
  let height = 700;
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({
   x: 0, y: 0, left: 0, top: 0, right: 375, bottom: height, width: 375, height, toJSON() {},
  }));
  try {
   const workspace = state(owner);
   workspace.viewport = { mode: 'fit', width: 375, height: 700 };
   workspace.editors = [{ id: 'field', type: 'text', value: '', label: 'Remote message', x: 10, y: 10, width: 200, height: 40 }];
   const view = await setup(workspace);
   mocks.config!.onFrame('AA==', { deviceWidth: 375, deviceHeight: 700, runtime_id: 'runtime', tab_id: 'tab' });
   await tick(); await Promise.resolve(); await tick();
   const input = view.getByRole('textbox', { name: 'Remote message' });
   const body = view.container.querySelector('.panel-body')!;
   // Focus before the initial fit timer expires, as on first takeover.
   input.focus();
   await tick();
   if (owner === 'agent') {
    const takeover = mocks.send.mock.calls.map(([p]) => p).find(p => (p as { op?: string }).op === 'takeover') as { id: string };
    expect(takeover).toBeTruthy();
    mocks.config!.onWorkspace?.({ ...workspace, control: state('human').control });
    mocks.config!.onCommandResult?.(takeover.id, true);
    await tick();
   }
   height = 400; // Android IME shrinks the WebView.
   observers.get(body)?.();
   await vi.advanceTimersByTimeAsync(150);
   expect(mocks.send.mock.calls.map(([p]) => p)).not.toContainEqual(expect.objectContaining({ op: 'viewport' }));
   expect(document.activeElement).toBe(input);
   input.blur(); await tick();
   await vi.advanceTimersByTimeAsync(150);
   expect(mocks.send).toHaveBeenCalledWith(expect.objectContaining({ op: 'viewport', args: { mode: 'fit', width: 375, height: 400 } }));
  } finally {
   cleanup(); vi.useRealTimers(); vi.restoreAllMocks();
  }
 });
});


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
