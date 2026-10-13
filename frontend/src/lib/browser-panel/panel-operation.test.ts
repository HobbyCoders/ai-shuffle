import { describe, it, expect, vi, afterEach } from 'vitest';
import { PanelOperation } from './panel-operation';
afterEach(() => vi.useRealTimers());
describe('panel operation scope', () => {
 it('waits for authoritative events, not elapsed time', async () => {
  const scope = new PanelOperation(); let ready = false; let resolved = false;
  const wait = scope.wait(() => ready, 'not ready').then(() => { resolved = true; });
  scope.notify(); await Promise.resolve(); expect(resolved).toBe(false);
  ready = true; scope.notify(); await wait; expect(resolved).toBe(true);
 });
 it('rejects outstanding and subsequent work on close', async () => {
  const scope = new PanelOperation();
  const wait = expect(scope.wait(() => false, 'timeout')).rejects.toThrow('closed');
  scope.close(); await wait;
  await expect(scope.wait(() => true, 'timeout')).rejects.toThrow('closed');
 });
 it('bounds waits and removes completed listeners', async () => {
  vi.useFakeTimers(); const scope = new PanelOperation(); const check = vi.fn(() => false);
  const wait = expect(scope.wait(check, 'Control was not granted', 50)).rejects.toThrow('Control was not granted');
  await vi.advanceTimersByTimeAsync(50); await wait;
  const calls = check.mock.calls.length; scope.notify(); expect(check).toHaveBeenCalledTimes(calls);
  expect(vi.getTimerCount()).toBe(0);
 });
 it('rejects ownership changes from the readiness predicate', async () => {
  const scope = new PanelOperation();
  await expect(scope.wait(() => { throw new Error('Another viewer controls'); }, 'timeout')).rejects.toThrow('Another viewer');
 });
});
