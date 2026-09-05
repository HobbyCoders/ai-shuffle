import { afterEach, describe, expect, it, vi } from 'vitest';
import { ResourceSync, RESOURCE_NAMES } from './resource-sync';
import { acceptUserEvent, resetUserEventDedupe } from './user-event-dedupe';
afterEach(() => { vi.useRealTimers(); resetUserEventDedupe(); });
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
 it('dedupes chat/global lifecycle side effects without dropping same-transport events', () => {
  const event={type:'user_session_cleared',session_id:'a'};
  expect(acceptUserEvent(event,'chat')).toBe(true); expect(acceptUserEvent(event,'global')).toBe(false);
  expect(acceptUserEvent(event,'global')).toBe(true); expect(acceptUserEvent(event,'global')).toBe(true);
  expect(acceptUserEvent({type:'delta',text:'a'},'chat')).toBe(true); expect(acceptUserEvent({type:'delta',text:'a'},'global')).toBe(true);
 });
});
