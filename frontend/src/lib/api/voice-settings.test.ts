import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
const identity = vi.hoisted(() => ({ epoch: 0, listeners: new Set<() => void>() }));
vi.mock('./client', () => ({ api: transport }));
vi.mock('$lib/stores/session-owner', () => ({
 sessionOwnerEpoch: () => identity.epoch,
 sessionOwnerBoundary: { subscribe(callback: () => void) {
  identity.listeners.add(callback); callback(); return () => identity.listeners.delete(callback);
 } }
}));

const settings = (voice = 'marin', minutes = 60) => ({ voice_mode_enabled: true,
 voice_model: 'gpt-realtime-2.1', voice_voice: voice, voice_provider_id: null,
 voice_max_call_minutes: minutes, voice_app_tools_enabled: false });
function deferred<T>() {
 let resolve!: (value: T) => void;
 const promise = new Promise<T>(r => { resolve = r; });
 return { promise, resolve };
}
function changeAccount() {
 identity.epoch++;
 for (const listener of identity.listeners) listener();
}
beforeEach(() => {
 vi.resetModules(); transport.get.mockReset(); transport.put.mockReset();
 identity.listeners.clear(); identity.epoch = 0;
});
afterEach(() => { vi.useRealTimers(); });

describe('voice settings synchronization', () => {
 it('reuses reads until a cross-worker settings invalidation or reconnect', async () => {
  vi.useFakeTimers();
  const voice = await import('./voice');
  const { resourceSync } = await import('$lib/services/resource-sync');
  transport.get.mockResolvedValueOnce(settings()).mockResolvedValueOnce(settings('cedar'));
  await voice.loadVoiceSettings(); await voice.loadVoiceSettings();
  expect(transport.get).toHaveBeenCalledTimes(1);
  resourceSync.invalidate(['settings']); await vi.advanceTimersByTimeAsync(100);
  expect((await voice.loadVoiceSettings()).voice_voice).toBe('cedar');
  transport.get.mockResolvedValueOnce(settings('marin', 90));
  resourceSync.invalidate(); await vi.advanceTimersByTimeAsync(100);
  expect((await voice.loadVoiceSettings()).voice_max_call_minutes).toBe(90);
  expect(transport.put).not.toHaveBeenCalled();
 });

 it('rejects an obsolete read without discarding the newer cached read', async () => {
  const voice = await import('./voice');
  const old = deferred<ReturnType<typeof settings>>();
  transport.get.mockReturnValueOnce(old.promise).mockResolvedValueOnce(settings('cedar'));
  const first = expect(voice.loadVoiceSettings()).rejects.toThrow('expired');
  await voice.loadVoiceSettings(true);
  old.resolve(settings()); await first;
  expect((await voice.loadVoiceSettings()).voice_voice).toBe('cedar');
  expect(transport.get).toHaveBeenCalledTimes(2);
 });

 it('retires reads and write responses belonging to the previous account', async () => {
  const voice = await import('./voice');
  const oldRead = deferred<ReturnType<typeof settings>>(), oldWrite = deferred<ReturnType<typeof settings>>();
  transport.get.mockReturnValueOnce(oldRead.promise).mockResolvedValueOnce(settings('cedar'));
  transport.put.mockReturnValueOnce(oldWrite.promise);
  const read = expect(voice.loadVoiceSettings()).rejects.toThrow('expired');
  const write = expect(voice.saveVoiceSettings({voice_voice:'marin'})).rejects.toThrow('expired');
  changeAccount();
  await voice.loadVoiceSettings();
  oldRead.resolve(settings()); oldWrite.resolve(settings()); await read; await write;
  expect((await voice.loadVoiceSettings()).voice_voice).toBe('cedar');
  expect(transport.get).toHaveBeenCalledTimes(2);
 });

 it('reconciles a peer change received while a local write was in flight', async () => {
  vi.useFakeTimers();
  const voice = await import('./voice');
  const { resourceSync } = await import('$lib/services/resource-sync');
  const pending = deferred<ReturnType<typeof settings>>();
  transport.put.mockReturnValueOnce(pending.promise);
  transport.get.mockResolvedValueOnce(settings('cedar', 90));
  const result = voice.saveVoiceSettings({voice_voice:'cedar'});
  resourceSync.invalidate(['settings']); await vi.advanceTimersByTimeAsync(100);
  pending.resolve(settings('cedar', 60));
  expect((await result).voice_max_call_minutes).toBe(90);
 });

 it('retries a failed read', async () => {
  const voice = await import('./voice');
  transport.get.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(settings());
  await expect(voice.loadVoiceSettings()).rejects.toThrow('offline');
  expect(await voice.loadVoiceSettings()).toEqual(settings());
 });

});
