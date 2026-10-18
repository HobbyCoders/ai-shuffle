import { afterEach, describe, expect, it, vi } from 'vitest';
import { ResourceSync, RESOURCE_NAMES } from './resource-sync';
import { acceptUserEvent, resetUserEventDedupe } from './user-event-dedupe';
const setHidden = (hidden: boolean, notify = true) => {
 Object.defineProperty(document, 'hidden', { configurable: true, value: hidden });
 Object.defineProperty(document, 'visibilityState', { configurable: true, value: hidden ? 'hidden' : 'visible' });
 if (notify) document.dispatchEvent(new Event('visibilitychange'));
};
afterEach(() => { vi.useRealTimers(); resetUserEventDedupe(); setHidden(false, false); });
describe('live resource invalidations', () => {
 it('coalesces bursts and refreshes only subscribed resources', async () => {
  vi.useFakeTimers(); const sync=new ResourceSync(), notes=vi.fn(), hooks=vi.fn();
  sync.subscribe(['notes'],notes); sync.subscribe(['hooks'],hooks);
  sync.invalidate(['notes']); sync.invalidate(['notes']); await vi.advanceTimersByTimeAsync(100);
  expect(notes).toHaveBeenCalledTimes(1); expect(hooks).not.toHaveBeenCalled();
 });
 it('reconnect refreshes every resource; unmounted views and logout discard queued work', async () => {
  vi.useFakeTimers(); const sync=new ResourceSync(); const fn=vi.fn(); const unmount=sync.subscribe(RESOURCE_NAMES,fn);
  sync.invalidate(); await vi.advanceTimersByTimeAsync(100); expect(fn).toHaveBeenCalledTimes(1);
  sync.invalidate(); sync.clear(); await vi.advanceTimersByTimeAsync(100); expect(fn).toHaveBeenCalledTimes(1);
  sync.invalidate(); unmount(); await vi.advanceTimersByTimeAsync(100); expect(fn).toHaveBeenCalledTimes(1);
 });
 it('does not lose an event delivered during a refresh', async () => {
  vi.useFakeTimers(); const sync=new ResourceSync(); let finish!:()=>void;
  const fn=vi.fn().mockImplementationOnce(()=>new Promise<void>(resolve=>{finish=resolve;})).mockResolvedValue(undefined);
  sync.subscribe(['settings'],fn); sync.invalidate(['settings']); await vi.advanceTimersByTimeAsync(100);
  sync.invalidate(['settings']); await vi.advanceTimersByTimeAsync(100); expect(fn).toHaveBeenCalledTimes(1);
  finish(); await Promise.resolve(); await Promise.resolve(); expect(fn).toHaveBeenCalledTimes(2);
 });
 it('keeps hidden invalidations dirty and refreshes each subscriber once on return (#1282)', async () => {
  vi.useFakeTimers(); const sync=new ResourceSync(), tags=vi.fn(), cache=vi.fn(), notes=vi.fn();
  sync.subscribe(['tags'],tags); sync.subscribe(['settings'],cache,{whileHidden:true}); sync.subscribe(['notes'],notes);
  setHidden(true);
  // Two server reconciles a minute apart while nobody can see the lists.
  sync.invalidate(); await vi.advanceTimersByTimeAsync(60_000);
  sync.invalidate(); await vi.advanceTimersByTimeAsync(60_000);
  expect(tags).not.toHaveBeenCalled(); expect(notes).not.toHaveBeenCalled();
  // A cache-only subscriber opted into hidden delivery.
  expect(cache).toHaveBeenCalledTimes(2);
  setHidden(false); await vi.advanceTimersByTimeAsync(100);
  expect(tags).toHaveBeenCalledTimes(1); expect(notes).toHaveBeenCalledTimes(1);
  // Nothing dirty: a later hide/return does not refresh again.
  setHidden(true); setHidden(false); await vi.advanceTimersByTimeAsync(100);
  expect(tags).toHaveBeenCalledTimes(1);
 });
 it('defers a follow-up that becomes due while hidden, and drops it on unmount or logout', async () => {
  vi.useFakeTimers(); const sync=new ResourceSync(); let finish!:()=>void;
  const fn=vi.fn().mockImplementationOnce(()=>new Promise<void>(resolve=>{finish=resolve;})).mockResolvedValue(undefined);
  sync.subscribe(['tags'],fn); sync.invalidate(['tags']); await vi.advanceTimersByTimeAsync(100);
  sync.invalidate(['tags']); setHidden(true);
  finish(); await vi.advanceTimersByTimeAsync(1000); expect(fn).toHaveBeenCalledTimes(1);
  setHidden(false); await vi.advanceTimersByTimeAsync(100); expect(fn).toHaveBeenCalledTimes(2);
  const other=vi.fn(); const unmount=sync.subscribe(['notes'],other);
  setHidden(true); sync.invalidate(); await vi.advanceTimersByTimeAsync(100);
  unmount(); sync.clear(); setHidden(false); await vi.advanceTimersByTimeAsync(100);
  expect(other).not.toHaveBeenCalled(); expect(fn).toHaveBeenCalledTimes(2);
 });
 it('dedupes chat/global lifecycle side effects without dropping same-transport events', () => {
  const event={type:'user_session_cleared',session_id:'a'};
  expect(acceptUserEvent(event,'chat')).toBe(true); expect(acceptUserEvent(event,'global')).toBe(false);
  expect(acceptUserEvent(event,'global')).toBe(true); expect(acceptUserEvent(event,'global')).toBe(true);
  expect(acceptUserEvent({type:'delta',text:'a'},'chat')).toBe(true); expect(acceptUserEvent({type:'delta',text:'a'},'global')).toBe(true);
 });
});
