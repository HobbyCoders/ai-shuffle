import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import BrowserPanel from './BrowserPanel.svelte';
import SessionSidePanel from '../layout/SessionSidePanel.test.svelte';
import type { BrowserStreamConfig } from '$lib/browser-panel/stream-client';
import type { BrowserWorkspaceState } from '$lib/browser-panel/workspace';

const mocks = vi.hoisted(() => ({
 config: null as BrowserStreamConfig | null,
 send: vi.fn((_payload: unknown) => true),
 sendMessage: vi.fn<(prompt: string) => Promise<boolean>>(),
 panel: { open: true, expanded: false, width: 500, status: 'live', url: 'https://example.test', pageLoading: false, inspectMode: false },
}));
vi.mock('$lib/browser-panel/stream-client', () => ({ createBrowserStreamClient: (config: BrowserStreamConfig) => {
 mocks.config = config;
 return Object.freeze({ connect: () => {}, destroy: () => {}, send: mocks.send, isConnected: () => true });
} }));
vi.mock('$lib/stores/browser-panel.svelte', () => ({ browserPanel: {
 forSession: () => mocks.panel, current: { defaultViewport: { mode: 'fixed', width: 1280, height: 800 } },
 setStatus: vi.fn(), setPage: vi.fn(), resetPageTracking: vi.fn(), setInspectMode: vi.fn(), setExpanded: vi.fn(),
} }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: { sendMessage: mocks.sendMessage, subscribe: (callback: (value: null) => void) => { callback(null); return () => {}; } } }));
vi.mock('$lib/api/browser-panel', () => ({
	startPanelBrowser: vi.fn(), uploadPanelFiles: vi.fn(),
	getBrowserTarget: vi.fn(async () => ({ browser_id: null, assigned: false, browsers: [] })), setBrowserTarget: vi.fn(),
}));

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
 mocks.sendMessage.mockReset().mockResolvedValue(true);
 mocks.panel.inspectMode = false;
 vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('design changes delivery', () => {
 const note = 'Make this button blue';
 /** The preview key the page hands out for each marked element. */
 const KEYS: Record<string, string> = { cta: 'k1', other: 'k2', late: 'k3', row: 'k4' };
 const hit = (id: string) => ({
  selector: `#${id}`, tag: 'button', id, classes: [], rect: { x: 0, y: 0, width: 80, height: 30 },
  key: KEYS[id], label: 'Save', styles: {}, similar: 1, tokens: [],
 });
 /** Mark an element on the page the way the stream reports a click in Design mode. */
 async function mark(id: string) {
  mocks.config!.onInspectHit?.('select', hit(id));
  await tick();
 }
 async function setupDesign() {
  mocks.panel.inspectMode = true;
  vi.stubGlobal('createImageBitmap', vi.fn().mockResolvedValue({ close: vi.fn() }));
  const view = await setup();
  // Marks are only taken for the page the panel is actually showing.
  mocks.config!.onFrame('AA==', { deviceWidth: 1280, deviceHeight: 800, runtime_id: 'runtime', tab_id: 'tab' });
  await tick();
  await mark('cta');
  await fireEvent.input(view.getByRole('textbox', { name: 'What should change?' }), { target: { value: note } });
  const send = () => view.container.querySelector('.send-btn') as HTMLButtonElement;
  return { ...view, send };
 }
 const sentStatus = (view: { container: Element }) => view.container.querySelector('.rail .sent');
 it('keeps the list when transport refuses, then allows a retry', async () => {
  mocks.sendMessage.mockResolvedValueOnce(false);
  const view = await setupDesign();
  expect(view.send().textContent).toContain('Send 1 change to the AI');
  await fireEvent.click(view.send());
  await waitFor(() => expect(view.getByRole('alert').textContent).toContain('Could not send'));
  expect(view.getByText(note)).toBeTruthy();
  expect(sentStatus(view)).toBeNull();
  expect(view.send().disabled).toBe(false);
  await fireEvent.click(view.send());
  await waitFor(() => expect(sentStatus(view)?.textContent).toContain('Sent 1 change'));
  expect(view.queryByText(note)).toBeNull();
  expect(mocks.sendMessage).toHaveBeenCalledTimes(2);
  expect(mocks.sendMessage.mock.calls[1][0]).toContain('- selector: #cta');
  expect(mocks.sendMessage.mock.calls[1][0]).toContain(`- request: ${note}`);
 });
 it('keeps the list and shows the error for a rejected send', async () => {
  mocks.sendMessage.mockRejectedValueOnce(new Error('Chat disconnected'));
  const view = await setupDesign();
  await fireEvent.click(view.send());
  await waitFor(() => expect(view.getByRole('alert').textContent).toContain('Chat disconnected'));
  expect(view.getByText(note)).toBeTruthy();
  expect(sentStatus(view)).toBeNull();
  expect(view.send().disabled).toBe(false);
 });
 it('sends every marked change in one message and puts the page back', async () => {
  const view = await setupDesign();
  await mark('other');
  await fireEvent.input(view.getByRole('textbox', { name: 'What should change?' }), { target: { value: 'More space above' } });
  expect(view.send().textContent).toContain('Send 2 changes to the AI');
  await fireEvent.click(view.send());
  await waitFor(() => expect(sentStatus(view)?.textContent).toContain('Sent 2 changes'));
  expect(mocks.sendMessage).toHaveBeenCalledOnce();
  const prompt = mocks.sendMessage.mock.calls[0][0];
  expect(prompt).toContain('I marked 2 changes');
  expect(prompt).toContain('- selector: #cta');
  expect(prompt).toContain('- selector: #other');
 });
 it('fixes the list while a send is pending', async () => {
  let accept!: (sent: boolean) => void;
  mocks.sendMessage.mockReturnValueOnce(new Promise(resolve => { accept = resolve; }));
  const view = await setupDesign();
  await fireEvent.click(view.send());
  expect(view.send().disabled).toBe(true);
  expect(view.send().textContent).toContain('Sending');
  expect(sentStatus(view)).toBeNull();
  await fireEvent.click(view.send());
  expect(mocks.sendMessage).toHaveBeenCalledOnce();
  // A change marked now would be cleared with the sent ones, unsent.
  await mark('late');
  expect(view.queryByRole('textbox', { name: 'What should change?' })).toBeNull();
  expect((view.getByRole('button', { name: 'Edit change 1' }) as HTMLButtonElement).disabled).toBe(true);
  expect((view.getByRole('button', { name: 'Remove change 1' }) as HTMLButtonElement).disabled).toBe(true);
  accept(true);
  await waitFor(() => expect(sentStatus(view)?.textContent).toContain('Sent 1 change'));
  expect(view.queryByText(note)).toBeNull();
  // The list is open again for the next change.
  await mark('late');
  expect(view.getByRole('textbox', { name: 'What should change?' })).toBeTruthy();
 });
 it('recovers the list and the send button when sender admission reaches its deadline', async () => {
  vi.useFakeTimers();
  try {
   // The sender owns the voice-context deadline; this is its caller contract.
   mocks.sendMessage.mockImplementationOnce(() => new Promise(resolve => setTimeout(() => resolve(false), 10_000)));
   const view = await setupDesign();
   await fireEvent.click(view.send());
   await vi.advanceTimersByTimeAsync(9_999);
   expect(view.send().disabled).toBe(true);
   expect(sentStatus(view)).toBeNull();
   await vi.advanceTimersByTimeAsync(1);
   await tick();
   expect(view.getByRole('alert').textContent).toContain('Could not send');
   expect(view.getByText(note)).toBeTruthy();
   expect(view.send().disabled).toBe(false);
   await fireEvent.click(view.send());
   await tick();
   expect(sentStatus(view)?.textContent).toContain('Sent 1 change');
  } finally {
   cleanup(); vi.useRealTimers();
  }
 });
 it('ignores a late acceptance after switching to another session', async () => {
  let accept!: (sent: boolean) => void;
  mocks.sendMessage.mockReturnValueOnce(new Promise(resolve => { accept = resolve; }));
  const view = await setupDesign();
  await fireEvent.click(view.send());
  await view.rerender({ sessionId: 'other-session' });
  accept(true);
  await tick();
  expect(sentStatus(view)).toBeNull();
  expect(view.queryByText(note)).toBeNull();
 });

 /** The design requests the panel made to the page, pin tracking aside. */
 const pageRequests = () => mocks.send.mock.calls
  .map(([payload]) => payload as Record<string, unknown>)
  .filter((payload) => payload.type === 'design' && payload.op !== 'rects');
 type View = Awaited<ReturnType<typeof setupDesign>>;
 /** Open the style controls of the change being edited, if they are closed. */
 async function openControls(view: View) {
  const toggle = view.getByRole('button', { name: 'Try it on the page' });
  if (toggle.getAttribute('aria-expanded') !== 'true') await fireEvent.click(toggle);
 }
 async function previewCorners(view: View) {
  await openControls(view);
  await fireEvent.input(view.getByLabelText('Corners'), { target: { value: '12' } });
  await waitFor(() => expect(pageRequests().at(-1)).toMatchObject({
   op: 'style', key: 'k1', selector: '#cta', scope: 'one', styles: { 'border-radius': '12px' },
  }));
 }
 it('puts the queued previews back on the page after the panel reconnects', async () => {
  const view = await setupDesign();
  await previewCorners(view);
  // The worker took this viewer's previews off when its connection dropped.
  mocks.config!.onClose?.(false);
  await tick();
  mocks.send.mockClear();
  mocks.config!.onOpen?.();
  await tick();
  expect(pageRequests()).toEqual([expect.objectContaining({
   op: 'style', key: 'k1', styles: { 'border-radius': '12px' },
  })]);
 });
 it('takes the previews off the page once the list is sent', async () => {
  const view = await setupDesign();
  await previewCorners(view);
  mocks.send.mockClear();
  await fireEvent.click(view.send());
  await waitFor(() => expect(sentStatus(view)?.textContent).toContain('Sent 1 change'));
  expect(pageRequests()).toEqual([expect.objectContaining({ op: 'reset' })]);
  expect(pageRequests()[0].key).toBeUndefined();
 });
 it('waits to take sent previews off until the page can be reached again', async () => {
  const view = await setupDesign();
  await previewCorners(view);
  // The page is showing another size than the panel knows: requests would be refused.
  mocks.config!.onWorkspace?.({ ...state('human'), viewport: { mode: 'fixed', width: 900, height: 700 } });
  await tick();
  mocks.send.mockClear();
  await fireEvent.click(view.send());
  await waitFor(() => expect(sentStatus(view)?.textContent).toContain('Sent 1 change'));
  expect(pageRequests()).toEqual([]);
  mocks.config!.onFrame('AA==', { deviceWidth: 900, deviceHeight: 700, runtime_id: 'runtime', tab_id: 'tab' });
  await tick();
  expect(pageRequests()).toEqual([expect.objectContaining({ op: 'reset' })]);
 });
 it('starts from a clean page when a row was removed while the page could not be reached', async () => {
  const view = await setupDesign();
  await previewCorners(view);
  await mark('other');
  await openControls(view);
  await fireEvent.input(view.getByLabelText('Corners'), { target: { value: '20' } });
  await waitFor(() => expect(pageRequests().at(-1)).toMatchObject({ op: 'style', key: 'k2' }));
  // The page changes size: nothing reaches it until a frame of that size arrives.
  mocks.config!.onWorkspace?.({ ...state('human'), viewport: { mode: 'fixed', width: 900, height: 700 } });
  await tick();
  mocks.send.mockClear();
  await fireEvent.click(view.getByRole('button', { name: 'Remove change 1' }));
  expect(pageRequests()).toEqual([]);
  mocks.config!.onFrame('AA==', { deviceWidth: 900, deviceHeight: 700, runtime_id: 'runtime', tab_id: 'tab' });
  await tick();
  // Everything comes off first, so the removed row's preview goes with it.
  expect(pageRequests().map((r) => [r.op, r.key])).toEqual([['reset', undefined], ['style', 'k2']]);
 });
 it('shows the latest values once another device returns control', async () => {
  const view = await setupDesign();
  await previewCorners(view);
  const held = state('human');
  held.control.viewer_id = 'someone-else';
  mocks.config!.onWorkspace?.(held);
  await tick();
  // The worker refuses previews while the other device has control.
  await fireEvent.input(view.getByLabelText('Corners'), { target: { value: '30' } });
  await waitFor(() => expect(pageRequests().at(-1)).toMatchObject({ styles: { 'border-radius': '30px' } }));
  mocks.send.mockClear();
  mocks.config!.onWorkspace?.(state('agent'));
  await tick();
  expect(pageRequests()).toEqual([
   expect.objectContaining({ op: 'reset' }),
   expect.objectContaining({ op: 'style', key: 'k1', styles: { 'border-radius': '30px' } }),
  ]);
 });
 it('drops the list when the browser shows another tab, even at the same address', async () => {
  const view = await setupDesign();
  await previewCorners(view);
  mocks.send.mockClear();
  const other = state('human');
  other.active_tab_id = 'tab-2';
  other.tabs = [{ id: 'tab-2', title: 'Example', url: 'https://example.test', current: true }];
  mocks.config!.onWorkspace?.(other);
  await tick();
  expect(view.queryByRole('textbox', { name: 'What should change?' })).toBeNull();
  mocks.config!.onFrame('AA==', { deviceWidth: 1280, deviceHeight: 800, runtime_id: 'runtime', tab_id: 'tab-2' });
  await tick();
  // Nothing of the old tab's list is put on this one.
  expect(pageRequests().filter((r) => r.op === 'style')).toEqual([]);
 });
 it('offers space between children only on an element that lays children out', async () => {
  const view = await setupDesign();
  await openControls(view);
  expect(view.getByLabelText('Corners')).toBeTruthy();
  expect(view.queryByLabelText('Space between')).toBeNull();
  mocks.config!.onInspectHit?.('select', { ...hit('row'), styles: { display: 'flex', gap: '8px' } });
  await tick();
  await openControls(view);
  await fireEvent.input(view.getByLabelText('Space between'), { target: { value: '24' } });
  await waitFor(() => expect(pageRequests().at(-1)).toMatchObject({ op: 'style', key: 'k4', styles: { gap: '24px' } }));
 });
});

describe('native keyboard in fit viewport', () => {
 it.each(['agent', 'human'] as const)('freezes remote resizing while focused and resyncs on blur (initial owner: %s)', async (owner) => {
  vi.useFakeTimers();
  const observers = new Map<Element, () => void>();
  vi.stubGlobal('ResizeObserver', class {
   target?: Element;
   constructor(private callback: () => void) {}
   observe(target: Element) { this.target = target; observers.set(target, this.callback); }
   unobserve(target: Element) { observers.delete(target); }
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

describe('pointer input on a phone-width page', () => {
 type Sent = Record<string, unknown>;
 function pointer(target: HTMLElement, type: string, init: { x?: number; y?: number; button?: number; buttons?: number; ctrlKey?: boolean; deltaY?: number } = {}) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.assign(event, {
   pointerType: 'mouse', pointerId: 7, clientX: init.x ?? 10, clientY: init.y ?? 10,
   button: init.button ?? -1, buttons: init.buttons ?? 0, deltaX: 0, deltaY: init.deltaY ?? 0,
   altKey: false, ctrlKey: init.ctrlKey ?? false, metaKey: false, shiftKey: false,
  });
  target.dispatchEvent(event);
 }
 const inputs = () => mocks.send.mock.calls.map(([p]) => p as Sent).filter((p) => p.type === 'input');
 const flush = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); await tick(); };
 async function setupPhone() {
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
  vi.stubGlobal('createImageBitmap', vi.fn().mockResolvedValue({ close: vi.fn(), width: 500, height: 700 }));
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({
   x: 0, y: 0, left: 0, top: 0, right: 375, bottom: 700, width: 375, height: 700, toJSON() {},
  }));
  const workspace = state('human');
  workspace.viewport = { mode: 'fixed', width: 375, height: 700 };
  const view = await setup(workspace);
  // The window cannot be narrower than about 500 px: the page is its left 375 px.
  mocks.config!.onFrame('AA==', { deviceWidth: 500, deviceHeight: 700, runtime_id: 'runtime', tab_id: 'tab' });
  await tick(); await Promise.resolve(); await tick();
  mocks.send.mockClear();
  return { view, surface: view.getByRole('application') };
 }
 async function answer(payload: Sent) { mocks.config!.onCommandResult?.(payload.id as string, true); await flush(); }
 afterEach(() => vi.restoreAllMocks());

 it('accepts the padded frame and keeps only the newest hover while one is on its way', async () => {
  const { view, surface } = await setupPhone();
  expect(view.queryByText(/waiting for the current frame/)).toBeNull();
  pointer(surface, 'pointermove', { x: 10 }); await flush();
  pointer(surface, 'pointermove', { x: 20 }); await flush();
  pointer(surface, 'pointermove', { x: 30 }); await flush();
  expect(inputs().map((p) => p.x)).toEqual([10]);
  await answer(inputs()[0]);
  expect(inputs().map((p) => p.x)).toEqual([10, 30]);
 });

 it('sends the last drag step before the release', async () => {
  const { surface } = await setupPhone();
  pointer(surface, 'pointerdown', { x: 10, button: 0, buttons: 1 }); await flush();
  pointer(surface, 'pointermove', { x: 20, buttons: 1 }); await flush();
  pointer(surface, 'pointermove', { x: 40, buttons: 1 }); await flush();
  pointer(surface, 'pointerup', { x: 40, button: 0, buttons: 0 }); await flush();
  expect(inputs().map((p) => [p.event, p.x])).toEqual([['down', 10], ['move', 20], ['move', 40], ['up', 40]]);
 });

 it('drops a held hover when a button is pressed', async () => {
  const { surface } = await setupPhone();
  pointer(surface, 'pointermove', { x: 10 }); await flush();
  pointer(surface, 'pointermove', { x: 20 }); await flush();
  pointer(surface, 'pointerdown', { x: 30, button: 0, buttons: 1 }); await flush();
  expect(inputs().map((p) => [p.event, p.x])).toEqual([['move', 10], ['down', 30]]);
 });

 it('adds up wheel steps only at the same spot with the same modifiers', async () => {
  const { surface } = await setupPhone();
  pointer(surface, 'wheel', { x: 10, deltaY: 100 }); await flush();
  pointer(surface, 'wheel', { x: 12, deltaY: 50 }); await flush();
  pointer(surface, 'wheel', { x: 12, deltaY: 25 }); await flush();
  pointer(surface, 'wheel', { x: 12, deltaY: 30, ctrlKey: true }); await flush();
  pointer(surface, 'wheel', { x: 300, y: 600, deltaY: 20 }); await flush();
  await answer(inputs().at(-1)!);
  // One scroll waits behind the one on its way; another spot or modifier
  // replaces it instead of retargeting it.
  expect(inputs().map((p) => [p.x, p.deltaY, p.modifiers])).toEqual([[10, 100, 0], [300, 20, 0]]);
  pointer(surface, 'wheel', { x: 12, deltaY: 50 }); await flush();
  pointer(surface, 'wheel', { x: 12, deltaY: 25 }); await flush();
  await answer(inputs().at(-1)!);
  expect(inputs().slice(2).map((p) => [p.x, p.deltaY])).toEqual([[12, 75]]);
 });

 it('sends at most one held scroll ahead of a tap, and nothing after the socket drops', async () => {
  const { surface } = await setupPhone();
  for (let i = 0; i < 20; i++) { pointer(surface, 'wheel', { x: 10 + i * 15, deltaY: 10 }); await flush(); }
  pointer(surface, 'pointerdown', { x: 50, button: 0, buttons: 1 }); await flush();
  expect(inputs().map((p) => p.event)).toEqual(['wheel', 'wheel', 'down']);
  mocks.send.mockClear();
  pointer(surface, 'wheel', { x: 10, deltaY: 10 }); await flush();
  pointer(surface, 'wheel', { x: 10, deltaY: 10 }); await flush();
  mocks.config!.onClose?.(false); await flush();
  mocks.config!.onOpen?.(); await flush();
  pointer(surface, 'pointerdown', { x: 50, button: 0, buttons: 1 }); await flush();
  // The two held scrolls were dropped with the old socket.
  expect(inputs().map((p) => p.event)).toEqual(['down']);
 });

 it('drops held scrolling when the page is navigated', async () => {
  const { view, surface } = await setupPhone();
  pointer(surface, 'wheel', { deltaY: 100 }); await flush();
  pointer(surface, 'wheel', { deltaY: 50 }); await flush();
  await fireEvent.click(view.getByRole('button', { name: 'Go back' })); await flush();
  expect(mocks.send).toHaveBeenCalledWith(expect.objectContaining({ op: 'command', args: { command: 'back' } }));
  await answer(inputs()[0]);
  expect(inputs().map((p) => p.deltaY)).toEqual([100]);
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

describe('Escape layering with the side panel', () => {
 it('Escape in the address input cancels the edit only; the next Escape closes the panel', async () => {
  // The side panel mounts first, so its window listener sees Escape first.
  const onClose = vi.fn();
  render(SessionSidePanel, { active: 'browser', expanded: false, width: 640, onClose, onResize: vi.fn(), onExpand: vi.fn() });
  const view = await setup();
  await fireEvent.click(view.container.querySelector('.address-display')!);
  const input = view.getByRole('textbox', { name: 'Page address' });
  await fireEvent.keyDown(input, { key: 'Escape' });
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(view.queryByRole('textbox', { name: 'Page address' })).toBeNull();
  expect(onClose).not.toHaveBeenCalled();
  await fireEvent.keyDown(window, { key: 'Escape' });
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(onClose).toHaveBeenCalledOnce();
 });
});
