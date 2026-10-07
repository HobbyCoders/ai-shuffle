import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import TerminalTaskStop from './TerminalTaskStop.svelte';

const api = vi.hoisted(() => ({ get: vi.fn(), stop: vi.fn() }));
vi.mock('$lib/api/sessions', () => ({ getSessionTaskStopState: api.get, stopSessionTask: api.stop }));
let intersect: (entries: { isIntersecting: boolean }[]) => void;
let hidden = false;
const disconnect = vi.fn();
const props = { parentSessionId: 'parent', taskId: 'child', canControl: true };
const button = () => screen.queryByRole('button', { name: 'Stop pending work' });

function deferred<T>() {
 let resolve!: (value: T) => void;
 let reject!: (reason: Error) => void;
 const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
 return { promise, resolve, reject };
}
async function visible() {
 intersect([{ isIntersecting: true }]);
 await tick();
}
async function visibility(value: boolean) {
 hidden = value;
 document.dispatchEvent(new Event('visibilitychange'));
 await tick();
}

beforeEach(() => {
 vi.clearAllMocks();
 hidden = false;
 vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden);
 vi.stubGlobal('IntersectionObserver', class {
  constructor(callback: typeof intersect) { intersect = callback; }
  observe() {}
  disconnect = disconnect;
 });
 api.get.mockResolvedValue({ eligible: true, turn_id: 'observed-turn' });
 api.stop.mockResolvedValue({ ok: true });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('TerminalTaskStop', () => {
 it('does not query historical cards until visible or advertise read-only controls', async () => {
  const { rerender } = render(TerminalTaskStop, { ...props, canControl: false });
  await visible();
  expect(api.get).not.toHaveBeenCalled();
  expect(button()).toBeNull();
  await rerender(props);
  await waitFor(() => expect(button()).not.toBeNull());
  expect(api.get).toHaveBeenCalledWith('parent', 'child', { signal: expect.any(AbortSignal) });
 });
 it('has no Stop before a fresh capability arrives', async () => {
  const pending = deferred<{ eligible: boolean; turn_id: string }>();
  api.get.mockReturnValue(pending.promise);
  render(TerminalTaskStop, props);
  expect(api.get).not.toHaveBeenCalled();
  await visible();
  expect(button()).toBeNull();
  pending.resolve({ eligible: true, turn_id: 'observed-turn' });
  await waitFor(() => expect(button()).not.toBeNull());
 });
 it.each([{ eligible: false }, { eligible: true }])('fails closed without a usable capability: %j', async state => {
  api.get.mockResolvedValue(state);
  render(TerminalTaskStop, props);
  await visible();
  expect(button()).toBeNull();
 });
 it('fails closed when eligibility cannot be read', async () => {
  api.get.mockRejectedValue(new Error('unreachable'));
  render(TerminalTaskStop, props);
  await visible();
  expect(button()).toBeNull();
 });
 it('sends exactly the displayed identity, disables duplicate Stop, and clears success', async () => {
  const pending = deferred<{ ok: boolean }>();
  api.stop.mockReturnValue(pending.promise);
  render(TerminalTaskStop, props);
  await visible();
  await waitFor(() => expect(button()).not.toBeNull());
  await fireEvent.click(button()!);
  const stopping = screen.getByRole('button', { name: 'Stopping…' });
  expect(stopping.hasAttribute('disabled')).toBe(true);
  await fireEvent.click(stopping);
  expect(api.stop).toHaveBeenCalledExactlyOnceWith('parent', 'child', 'observed-turn');
  pending.resolve({ ok: true });
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Stopping…' })).toBeNull());
  expect(button()).toBeNull();
 });
 it('does not restore an in-flight eligibility response after successful Stop', async () => {
  vi.useFakeTimers();
  const stale = deferred<{ eligible: boolean; turn_id: string }>();
  api.get.mockResolvedValue({ eligible: false })
   .mockResolvedValueOnce({ eligible: true, turn_id: 'observed-turn' })
   .mockReturnValueOnce(stale.promise);
  render(TerminalTaskStop, props);
  await visible();
  await vi.advanceTimersByTimeAsync(0);
  expect(button()).not.toBeNull();
  await vi.advanceTimersByTimeAsync(10000);
  expect(api.get).toHaveBeenCalledTimes(2);
  await fireEvent.click(button()!);
  await vi.advanceTimersByTimeAsync(0);
  expect(api.stop).toHaveBeenCalledExactlyOnceWith('parent', 'child', 'observed-turn');
  expect(button()).toBeNull();
  stale.resolve({ eligible: true, turn_id: 'observed-turn' });
  await stale.promise;
  await vi.advanceTimersByTimeAsync(0);
  await tick();
  expect(button()).toBeNull();
 });
 it('shows stale refusal and rechecks instead of issuing an unscoped stop', async () => {
  api.stop.mockResolvedValue({ ok: false, detail: 'stop target changed' });
  render(TerminalTaskStop, props);
  await visible();
  await waitFor(() => expect(button()).not.toBeNull());
  await fireEvent.click(button()!);
  await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
  expect(api.stop).toHaveBeenCalledExactlyOnceWith('parent', 'child', 'observed-turn');
  api.get.mockResolvedValue({ eligible: false });
  await fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  expect(button()).toBeNull();
  expect(api.stop).toHaveBeenCalledTimes(1);
 });
 it('aborts and ignores an eligibility response for the previous child', async () => {
  const old = deferred<{ eligible: boolean; turn_id: string }>();
  api.get.mockReturnValueOnce(old.promise).mockResolvedValue({ eligible: false });
  const { rerender } = render(TerminalTaskStop, props);
  await visible();
  const signal = api.get.mock.calls[0][2].signal;
  await rerender({ ...props, taskId: 'new-child' });
  expect(signal.aborted).toBe(true);
  old.resolve({ eligible: true, turn_id: 'old-turn' });
  await old.promise;
  await tick();
  expect(button()).toBeNull();
  expect(api.get).toHaveBeenLastCalledWith('parent', 'new-child', { signal: expect.any(AbortSignal) });
 });
 it('ignores a delayed Stop response after navigating to a different child', async () => {
  const old = deferred<{ ok: boolean }>();
  api.stop.mockReturnValue(old.promise);
  const { rerender } = render(TerminalTaskStop, props);
  await visible();
  await waitFor(() => expect(button()).not.toBeNull());
  await fireEvent.click(button()!);
  api.get.mockResolvedValue({ eligible: true, turn_id: 'new-turn' });
  await rerender({ ...props, taskId: 'new-child' });
  await waitFor(() => expect(button()).not.toBeNull());
  old.resolve({ ok: false });
  await old.promise;
  await tick();
  expect(screen.queryByRole('alert')).toBeNull();
  await fireEvent.click(button()!);
  expect(api.stop).toHaveBeenLastCalledWith('parent', 'new-child', 'new-turn');
 });
 it('does not restore an old in-flight capability when the document is hidden', async () => {
  const pending = deferred<{ eligible: boolean; turn_id: string }>();
  api.get.mockReturnValueOnce(pending.promise).mockResolvedValue({ eligible: false });
  render(TerminalTaskStop, props);
  await visible();
  await visibility(true);
  pending.resolve({ eligible: true, turn_id: 'hidden-old-turn' });
  await pending.promise;
  await tick();
  expect(button()).toBeNull();
  await visibility(false);
  expect(button()).toBeNull();
 });
 it('does not reuse a request started before a hidden-to-visible transition', async () => {
  const pending = deferred<{ eligible: boolean; turn_id: string }>();
  api.get.mockReturnValueOnce(pending.promise).mockResolvedValue({ eligible: false });
  render(TerminalTaskStop, props);
  await visible();
  await visibility(true);
  await visibility(false);
  pending.resolve({ eligible: true, turn_id: 'pre-hide-turn' });
  await pending.promise;
  await tick();
  expect(button()).toBeNull();
 });
 it('does not clear a fresh post-show capability when the old GET rejects', async () => {
  const old = deferred<{ eligible: boolean; turn_id: string }>();
  api.get.mockReturnValueOnce(old.promise)
   .mockResolvedValue({ eligible: true, turn_id: 'post-show-turn' });
  render(TerminalTaskStop, props);
  await visible();
  await visibility(true);
  await visibility(false);
  await waitFor(() => expect(button()).not.toBeNull());
  old.reject(new Error('obsolete request failed'));
  await old.promise.catch(() => {});
  await tick();
  expect(button()).not.toBeNull();
  await fireEvent.click(button()!);
  expect(api.stop).toHaveBeenCalledExactlyOnceWith('parent', 'child', 'post-show-turn');
 });
 it('clears capability while hidden and disconnects and aborts on unmount', async () => {
  vi.useFakeTimers();
  const pending = deferred<{ eligible: boolean; turn_id: string }>();
  const postShow = deferred<{ eligible: boolean; turn_id: string }>();
  api.get.mockResolvedValueOnce({ eligible: true, turn_id: 'observed-turn' })
   .mockReturnValueOnce(pending.promise).mockReturnValueOnce(postShow.promise);
  const { unmount } = render(TerminalTaskStop, props);
  await visible();
  await vi.advanceTimersByTimeAsync(0);
  expect(button()).not.toBeNull();
  await vi.advanceTimersByTimeAsync(10000);
  expect(api.get).toHaveBeenCalledTimes(2);
  const signal = api.get.mock.calls[1][2].signal;
  await visibility(true);
  expect(button()).toBeNull();
  expect(signal.aborted).toBe(true);
  await visibility(false);
  const postShowSignal = api.get.mock.calls[2][2].signal;
  expect(postShowSignal.aborted).toBe(false);
  unmount();
  expect(postShowSignal.aborted).toBe(true);
  pending.resolve({ eligible: true, turn_id: 'obsolete-hidden-turn' });
  postShow.resolve({ eligible: true, turn_id: 'obsolete-unmounted-turn' });
  await vi.advanceTimersByTimeAsync(20000);
  await tick();
  expect(button()).toBeNull();
  expect(api.get).toHaveBeenCalledTimes(3);
  expect(disconnect).toHaveBeenCalledOnce();
 });
});
