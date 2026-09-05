import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor, within } from '@testing-library/svelte';
import OpenAICredits from './OpenAICredits.svelte';
import { tick } from 'svelte';
import type { ModelProvider } from '$lib/api/model-providers';
import * as billing from '$lib/api/openai-billing';
import { createBrowserStreamClient } from '$lib/browser-panel/stream-client';
const mocks = vi.hoisted(() => ({ connect: vi.fn(), destroy: vi.fn(), send: vi.fn() }));
vi.mock('$lib/api/openai-billing', () => ({ getOpenAIBillingStatus: vi.fn(), connectOpenAIBilling: vi.fn(), confirmOpenAIBilling: vi.fn(), cancelOpenAIBilling: vi.fn(), disconnectOpenAIBilling: vi.fn() }));
vi.mock('$lib/browser-panel/stream-client', () => ({ createBrowserStreamClient: vi.fn(() => mocks) }));
const provider = { id: 'p1', name: 'OpenAI', enabled: true, provider_type: 'openai', base_url: 'https://api.openai.com/v1' } as ModelProvider;
const initial: billing.OpenAIBillingStatus = { status: 'not_connected', flow_id: null, observation_id: null, organization_id: null, credits: null, fetched_at: null, error_kind: null, owner_instance_id: null, browser_available: true };
const ready: billing.OpenAIBillingStatus = { ...initial, status: 'ready', flow_id: 'flow-1', observation_id: 'obs-1', organization_id: 'org-example', credits: { balance_remaining: 21.09 }, fetched_at: 1 };
beforeEach(() => {
 vi.clearAllMocks();
 vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
 HTMLDialogElement.prototype.showModal = function () { this.open = true; };
 HTMLDialogElement.prototype.close = function () { this.open = false; };
 vi.mocked(billing.getOpenAIBillingStatus).mockResolvedValue(initial);
 vi.mocked(billing.connectOpenAIBilling).mockResolvedValue(ready);
 vi.mocked(billing.cancelOpenAIBilling).mockResolvedValue(initial);
 vi.mocked(billing.confirmOpenAIBilling).mockResolvedValue({ ...ready, status: 'connected' });
 vi.mocked(billing.disconnectOpenAIBilling).mockResolvedValue(initial);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
async function start() {
 const view = render(OpenAICredits, { provider });
 await fireEvent.click(await screen.findByRole('button', { name: 'Sign in for credits' }));
 await screen.findByRole('button', { name: 'Confirm organization' });
 return view;
}
describe('credit sign-in keyboard control', () => {
 beforeEach(() => {
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
  vi.stubGlobal('createImageBitmap', vi.fn().mockResolvedValue({ close: vi.fn() }));
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 300, height: 500 } as DOMRect);
 });
 async function live() {
  await start();
  const config = vi.mocked(createBrowserStreamClient).mock.calls.at(-1)![0];
  expect((screen.getByRole('button', { name: 'Show keyboard' }) as HTMLButtonElement).disabled).toBe(true);
  config.onStatus('live', null);
  await tick();
  expect((screen.getByRole('button', { name: 'Show keyboard' }) as HTMLButtonElement).disabled).toBe(true);
  config.onFrame('AA==', { deviceWidth: 300, deviceHeight: 500 });
  await tick(); await Promise.resolve();
  return { config, input: screen.getByRole('textbox', { name: 'Type in the remote browser' }) as HTMLTextAreaElement, surface: screen.getByRole('application') };
 }
 function tap(surface: HTMLElement, pointerType: string) {
  for (const type of ['pointerdown', 'pointerup']) {
   const event = new Event(type, { bubbles: true, cancelable: true });
   Object.assign(event, { pointerType, pointerId: 1, clientX: 50, clientY: 60 });
   surface.dispatchEvent(event);
  }
 }
 it.each(['touch', 'mouse'])('opens only explicitly, forwards text and preserves %s page-tap focus until Hide', async pointerType => {
  const { input, surface } = await live();
  const show = screen.getByRole('button', { name: 'Show keyboard' });
  expect(show.closest('header')).not.toBeNull();
  expect(surface.contains(show)).toBe(false);
  expect(show.getAttribute('aria-pressed')).toBe('false');
  expect(document.activeElement).not.toBe(input);
  tap(surface, pointerType);
  expect(document.activeElement).not.toBe(input);
  expect(mocks.send).toHaveBeenCalledWith(expect.objectContaining({ type: 'input', kind: 'mouse', event: 'down' }));
  await fireEvent.click(show);
  expect(document.activeElement).toBe(input);
  const hide = screen.getByRole('button', { name: 'Hide keyboard' });
  expect(hide.getAttribute('aria-pressed')).toBe('true');
  tap(surface, pointerType);
  expect(document.activeElement).toBe(input);
  mocks.send.mockClear();
  input.value = 'person@example.com';
  await fireEvent.input(input, { data: 'person@example.com', inputType: 'insertText' });
  expect(mocks.send).toHaveBeenCalledExactlyOnceWith({ type: 'input', kind: 'text', text: 'person@example.com' });
  const down = new Event('pointerdown', { bubbles: true, cancelable: true });
  hide.dispatchEvent(down);
  expect(down.defaultPrevented).toBe(true);
  expect(document.activeElement).toBe(input);
  await fireEvent.click(hide);
  expect(document.activeElement).not.toBe(input);
  expect(screen.getByRole('button', { name: 'Show keyboard' }).getAttribute('aria-pressed')).toBe('false');
  tap(surface, pointerType);
  expect(document.activeElement).not.toBe(input);
 });
 it.each(['connecting', 'disconnected', 'browser_off', 'no_page'] as const)('dismisses and disables on %s without reopening on recovery', async status => {
  const { config, input } = await live();
  await fireEvent.click(screen.getByRole('button', { name: 'Show keyboard' }));
  config.onStatus(status, null); await tick();
  expect(document.activeElement).not.toBe(input);
  expect(input.disabled).toBe(true);
  expect((screen.getByRole('button', { name: 'Show keyboard' }) as HTMLButtonElement).disabled).toBe(true);
  mocks.send.mockClear();
  await fireEvent.input(input, { data: 'blocked', inputType: 'insertText' });
  expect(mocks.send).not.toHaveBeenCalled();
  config.onStatus('live', null); await tick();
  expect(input.disabled).toBe(false);
  expect(document.activeElement).not.toBe(input);
 });
 it('dismisses while confirmation is busy and stays closed after failure', async () => {
  const { input } = await live();
  let reject!: (error: Error) => void;
  vi.mocked(billing.confirmOpenAIBilling).mockReturnValue(new Promise((_, fail) => { reject = fail; }));
  vi.mocked(billing.getOpenAIBillingStatus).mockResolvedValue(ready);
  await fireEvent.click(screen.getByRole('button', { name: 'Show keyboard' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm organization' }));
  expect(document.activeElement).not.toBe(input);
  expect(input.disabled).toBe(true);
  expect((screen.getByRole('button', { name: 'Show keyboard' }) as HTMLButtonElement).disabled).toBe(true);
  reject(new Error('failed')); await tick(); await tick();
  expect(input.disabled).toBe(false);
  expect(document.activeElement).not.toBe(input);
 });
 it('dismisses and disables when polling loses the owned flow', async () => {
  const { input } = await live();
  await fireEvent.click(screen.getByRole('button', { name: 'Show keyboard' }));
  vi.mocked(billing.getOpenAIBillingStatus).mockResolvedValue(initial);
  await waitFor(() => expect(screen.queryByRole('textbox')).toBeNull(), { timeout: 3000 });
  expect(document.activeElement).not.toBe(input);
  expect((screen.getByRole('button', { name: 'Show keyboard' }) as HTMLButtonElement).disabled).toBe(true);
 });
});
describe('OpenAI credit sign-in', () => {
 it('resends the viewport after the login handshake reaches live', async () => {
  await start();
  const config = vi.mocked(createBrowserStreamClient).mock.calls.at(-1)![0];
  expect(config.providerId).toBe('p1');
  expect(config.sessionId).toBeUndefined();
  const bounds = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ width: 800, height: 600 } as DOMRect);
  try {
   mocks.send.mockClear();
   config.onStatus('live', null);
   expect(mocks.send).toHaveBeenCalledWith({ type: 'viewport', width: 800, height: 600 });
  } finally { bounds.mockRestore(); }
 });
 it.each(['connected', 'other_installation', 'reconnect_required', 'unavailable'] as const)('shows the saved balance and millisecond timestamp for %s', async status => {
  const fetchedAt = Date.UTC(2026, 8, 5, 12, 30);
  vi.mocked(billing.getOpenAIBillingStatus).mockResolvedValue({ ...ready, status, credits: { balance_remaining: 20.9104824 }, fetched_at: fetchedAt });
  render(OpenAICredits, { provider });
  const section = within(screen.getByRole('region', { name: 'OpenAI credits' }));
  const stale = status === 'reconnect_required' || status === 'unavailable';
  await section.findByText(stale ? 'Last known balance: $20.91 (stale)' : '$20.91 available');
  const time = section.getByText(new Date(fetchedAt).toLocaleString());
  expect(time.getAttribute('datetime')).toBe(new Date(fetchedAt).toISOString());
 });
 it('shows a zero balance and an epoch timestamp', async () => {
  vi.mocked(billing.getOpenAIBillingStatus).mockResolvedValue({ ...ready, status: 'connected', credits: { balance_remaining: 0 }, fetched_at: 0 });
  render(OpenAICredits, { provider });
  await screen.findByText('$0.00 available');
  expect(screen.getByText(new Date(0).toLocaleString()).getAttribute('datetime')).toBe(new Date(0).toISOString());
 });
 it('keeps the candidate balance inside the dialog until confirmation', async () => {
  await start();
  const section = within(screen.getByRole('region', { name: 'OpenAI credits' }));
  expect(section.queryByText(/available|Last checked|Last known balance/)).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm organization' }));
  await section.findByText('$21.09 available');
 });
 it.each(['reconnect_required', 'unavailable', 'not_connected'] as const)('can close after polling clears an expired flow (%s)', async status => {
  vi.useFakeTimers();
  const { tick } = await import('svelte');
  render(OpenAICredits, { provider });
  await vi.advanceTimersByTimeAsync(0); await tick();
  await fireEvent.click(screen.getByRole('button', { name: 'Sign in for credits' }));
  vi.mocked(billing.getOpenAIBillingStatus).mockResolvedValue({ ...initial, status });
  await vi.advanceTimersByTimeAsync(2000); await tick();
  expect(screen.getByText(/This sign-in is no longer active/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Confirm organization' })).toBeNull();
  expect(mocks.destroy).toHaveBeenCalled();
  await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(billing.cancelOpenAIBilling).not.toHaveBeenCalled();
  const calls = vi.mocked(billing.getOpenAIBillingStatus).mock.calls.length;
  await vi.advanceTimersByTimeAsync(6000);
  expect(billing.getOpenAIBillingStatus).toHaveBeenCalledTimes(calls);
 });
 it('requires explicit organization confirmation and closes without cancelling a confirmed binding', async () => {
  const view = await start();
  expect(screen.getByText('org-example')).toBeTruthy();
  expect(screen.getByText('$21.09 available')).toBeTruthy();
  expect(billing.confirmOpenAIBilling).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm organization' }));
  await waitFor(() => expect(billing.confirmOpenAIBilling).toHaveBeenCalledWith('p1', { flow_id: 'flow-1', observation_id: 'obs-1', organization_id: 'org-example' }));
  await screen.findByText('Connected');
  view.unmount();
  expect(billing.cancelOpenAIBilling).not.toHaveBeenCalled();
  expect(mocks.destroy).toHaveBeenCalled();
 });
 it('cancels only the owned unconfirmed flow on close', async () => {
  await start();
  await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  await waitFor(() => expect(billing.cancelOpenAIBilling).toHaveBeenCalledWith('p1', 'flow-1'));
 });
 it('cancels the owned flow when the provider panel unmounts', async () => {
  const view = await start(); view.unmount();
  expect(billing.cancelOpenAIBilling).toHaveBeenCalledWith('p1', 'flow-1');
 });
 it('offers no takeover on another installation', async () => {
  vi.mocked(billing.getOpenAIBillingStatus).mockResolvedValue({ ...initial, status: 'other_installation', owner_instance_id: 'other-host' });
  render(OpenAICredits, { provider });
  await screen.findByText('Connected on another installation');
  expect(screen.queryByRole('button', { name: 'Sign in for credits' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Disconnect credits' })).toBeNull();
 });
 it('explains browser availability and disables sign-in when stopped', async () => {
  vi.mocked(billing.getOpenAIBillingStatus).mockResolvedValue({ ...initial, browser_available: false });
  render(OpenAICredits, { provider });
  const button = await screen.findByRole('button', { name: 'Sign in for credits' });
  expect((button as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText(/Start the app’s browser/)).toBeTruthy();
 });
 it('requires a separate disconnect confirmation', async () => {
  vi.mocked(billing.getOpenAIBillingStatus).mockResolvedValue({ ...ready, status: 'connected' });
  render(OpenAICredits, { provider });
  await fireEvent.click(await screen.findByRole('button', { name: 'Disconnect credits' }));
  expect(billing.disconnectOpenAIBilling).not.toHaveBeenCalled();
  await fireEvent.click(screen.getAllByRole('button', { name: 'Disconnect credits' })[1]);
  await waitFor(() => expect(billing.disconnectOpenAIBilling).toHaveBeenCalledWith('p1'));
 });
 it('asks before starting a reconnect and leaves the current binding in place on cancel', async () => {
  vi.mocked(billing.getOpenAIBillingStatus).mockResolvedValue({ ...ready, status: 'connected' });
  render(OpenAICredits, { provider });
  await fireEvent.click(await screen.findByRole('button', { name: 'Reconnect credits' }));
  expect(billing.connectOpenAIBilling).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole('button', { name: 'Continue to sign-in' }));
  await screen.findByRole('button', { name: 'Confirm organization' });
  await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  expect(billing.disconnectOpenAIBilling).not.toHaveBeenCalled();
 });
 it('requires sign-in after a disconnect even if provider props contain the old binding', async () => {
  vi.mocked(billing.getOpenAIBillingStatus).mockResolvedValue({ ...ready, status: 'connected' });
  render(OpenAICredits, { provider: { ...provider, openai_billing_org_id: 'old-org' } });
  await fireEvent.click(await screen.findByRole('button', { name: 'Disconnect credits' }));
  await fireEvent.click(screen.getAllByRole('button', { name: 'Disconnect credits' })[1]);
  await screen.findByRole('button', { name: 'Sign in for credits' });
  expect(screen.queryByRole('button', { name: 'Reconnect credits' })).toBeNull();
 });
 it('polls only while the login dialog is open and stops on close', async () => {
  vi.useFakeTimers();
  const { tick } = await import('svelte');
  render(OpenAICredits, { provider });
  await vi.advanceTimersByTimeAsync(0); await tick();
  await vi.advanceTimersByTimeAsync(5000);
  expect(billing.getOpenAIBillingStatus).toHaveBeenCalledTimes(1);
  vi.mocked(billing.getOpenAIBillingStatus).mockResolvedValue(ready);
  await fireEvent.click(screen.getByRole('button', { name: 'Sign in for credits' }));
  await vi.advanceTimersByTimeAsync(4000);
  expect(billing.getOpenAIBillingStatus).toHaveBeenCalledTimes(3);
  await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  await vi.advanceTimersByTimeAsync(6000);
  expect(billing.getOpenAIBillingStatus).toHaveBeenCalledTimes(3);
 });
 it('does not display raw server errors', async () => {
  vi.mocked(billing.connectOpenAIBilling).mockRejectedValue({ detail: 'private upstream text' });
  render(OpenAICredits, { provider });
  await fireEvent.click(await screen.findByRole('button', { name: 'Sign in for credits' }));
  await screen.findByRole('alert');
  expect(screen.queryByText(/private upstream text/)).toBeNull();
 });
});
