import { afterEach, describe, it, expect, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: mocks }));
import { hydrateModelRegistry, getProviders, isRegistryHydrated } from './models';
import { effortRegistryFixture } from '$lib/test-stubs/effort-registry';
const payload = (id: string, models: object[]) => ({ ...effortRegistryFixture, providers: [{ id, name: id, provider_type: 'openrouter', models }] });
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

describe('capability migration hydration', () => {
 it('recovers canonical version-one null levels once without changing known model metadata', async () => {
  mocks.post.mockClear();
  let finish!: (result: { refreshed: string[]; failed: string[] }) => void;
  mocks.post.mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const live = effortRegistryFixture.providers.find(p => p.id === 'codex')!;
  const unknown = { version: 1, effort_levels: null, default_effort: null };
  const broken = live.models.map(model => model.id === 'gpt-5.6-luna' ? model : {
   ...model, effort_levels: null, default_effort: null, reasoning_capabilities: unknown
  });
  mocks.get.mockResolvedValue({ ...effortRegistryFixture, providers: [{ ...live, id: 'v1-null', models: broken }] });
  expect(await hydrateModelRegistry()).toBe(true);
  await hydrateModelRegistry();
  expect(mocks.post).toHaveBeenCalledTimes(1);
  expect(mocks.post).toHaveBeenCalledWith('/model-providers/refresh-capabilities', { provider_ids: ['v1-null'] }, { signal: expect.any(AbortSignal) });
  expect(getProviders()[0].models.find(m => m.id === 'gpt-5.6-luna')).toMatchObject(live.models[3]);
  mocks.get.mockResolvedValue({ ...effortRegistryFixture, providers: [{ ...live, id: 'v1-null' }] });
  finish({ refreshed: ['v1-null'], failed: [] });
  await vi.waitFor(() => expect(getProviders()[0].models[0].effort_levels).toContain('ultra'));
  expect(getProviders()[0].models.find(m => m.id === 'gpt-5.6-sol')?.default_effort).toBe('low');
  await hydrateModelRegistry();
  expect(mocks.post).toHaveBeenCalledTimes(1);
 });
 it('attempts a still-unknown version-one provider only once per page', async () => {
  mocks.post.mockClear().mockResolvedValue({ refreshed: ['v1-unknown'], failed: [] });
  mocks.get.mockResolvedValue(payload('v1-unknown', [{ id: 'model', effort_levels: null, default_effort: null, effort_metadata_version: 1,
   reasoning_capabilities: { version: 1, effort_levels: null, default_effort: null } }]));
  const count = mocks.get.mock.calls.length;
  await hydrateModelRegistry();
  await vi.waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(count + 2));
  await hydrateModelRegistry();
  await hydrateModelRegistry();
  expect(mocks.post).toHaveBeenCalledTimes(1);
  expect(getProviders()[0].models[0].effort_levels).toBeNull();
 });
 it('does not refresh known version-one capabilities, including empty arrays', async () => {
  mocks.post.mockClear();
  mocks.get.mockResolvedValue(payload('v1-known', [
   { id: 'glm', effort_levels: ['low', 'high', 'max'], default_effort: 'max', effort_metadata_version: 1 },
   { id: 'unsupported', effort_levels: [], default_effort: null, effort_metadata_version: 1 }
  ]));
  await hydrateModelRegistry();
  expect(mocks.post).not.toHaveBeenCalled();
 });
 it('makes the initial registry ready without waiting and refreshes each stale provider once', async () => {
  mocks.post.mockClear();
  let finish!: (result: { refreshed: string[]; failed: string[] }) => void;
  mocks.post.mockReturnValue(new Promise(resolve => { finish = resolve; }));
  mocks.get.mockResolvedValue(payload('stale-once', [{ id: 'model' }]));
  expect(await hydrateModelRegistry()).toBe(true);
  expect(isRegistryHydrated()).toBe(true);
  await hydrateModelRegistry();
  expect(mocks.post).toHaveBeenCalledTimes(1);
  expect(mocks.post).toHaveBeenCalledWith('/model-providers/refresh-capabilities', { provider_ids: ['stale-once'] }, { signal: expect.any(AbortSignal) });
  const count = mocks.get.mock.calls.length;
  finish({ refreshed: ['stale-once'], failed: [] });
  await vi.waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(count + 1));
  expect(mocks.post).toHaveBeenCalledTimes(1); // even when refresh returned stale data
 });
 it('retains state on failure without immediately looping, and accepts a later hydration', async () => {
  mocks.post.mockClear().mockRejectedValue(new Error('offline'));
  mocks.get.mockResolvedValue(payload('failure', [{ id: 'model', effort_levels: null }]));
  expect(await hydrateModelRegistry()).toBe(true);
  await hydrateModelRegistry();
  expect(getProviders()[0].models[0].id).toBe('model');
  expect(mocks.post).toHaveBeenCalledTimes(1);
  mocks.get.mockResolvedValue(payload('failure', [{ id: 'model', effort_levels: ['none', 'high'], default_effort: 'none', effort_metadata_version: 1 }]));
  await hydrateModelRegistry();
  expect(getProviders()[0].models[0].default_effort).toBe('none');
  expect(mocks.post).toHaveBeenCalledTimes(1);
 });
 it.each([{ effort_levels: ['low', 'high', 'max'] }, { effort_levels: [] }])('preserves known levels $effort_levels on same-version unknown hydration', async ({ effort_levels }) => {
  const canonical = { version: 1, effort_levels, default_effort: effort_levels.includes('max') ? 'max' : null, mandatory: true };
  mocks.get.mockResolvedValue(payload('unknown-refresh', [{ id: 'glm', ...canonical, effort_metadata_version: 1, reasoning_capabilities: canonical }]));
  await hydrateModelRegistry();
  for (const unknown of [null, undefined]) {
   mocks.get.mockResolvedValue(payload('unknown-refresh', [{ id: 'glm', label: 'Updated label', effort_levels: unknown, default_effort: null, effort_metadata_version: 1, reasoning_capabilities: { version: 1, effort_levels: null, default_effort: null } }]));
   await hydrateModelRegistry();
   expect(getProviders()[0].models[0]).toMatchObject({ label: 'Updated label', effort_levels, default_effort: canonical.default_effort, effort_metadata_version: 1, reasoning_capabilities: canonical });
  }
 });
 it.each([{ effort_levels: [] }, { effort_levels: ['low', 'high'] }])('accepts fresh exact levels $effort_levels that remove previous support', async ({ effort_levels }) => {
  const saved = { version: 1, effort_levels: ['low', 'high', 'max'], default_effort: 'max' };
  mocks.get.mockResolvedValue(payload('known-refresh', [{ id: 'glm', ...saved, effort_metadata_version: 1, reasoning_capabilities: saved }]));
  await hydrateModelRegistry();
  const fresh = { version: 1, effort_levels, default_effort: effort_levels.includes('high') ? 'high' : null };
  mocks.get.mockResolvedValue(payload('known-refresh', [{ id: 'glm', ...fresh, effort_metadata_version: 1, reasoning_capabilities: fresh }]));
  await hydrateModelRegistry();
  expect(getProviders()[0].models[0]).toMatchObject({ effort_levels, default_effort: fresh.default_effort, effort_metadata_version: 1, reasoning_capabilities: fresh });
 });
 it('does not overwrite normalized metadata with a stale hydration response', async () => {
  const canonical = { version: 1, effort_levels: ['low', 'high', 'max'], default_effort: 'max' };
  mocks.get.mockResolvedValue(payload('race', [{ id: 'glm', ...canonical, effort_metadata_version: 1, reasoning_capabilities: canonical }]));
  await hydrateModelRegistry();
  mocks.get.mockResolvedValue(payload('race', [{ id: 'glm', effort_levels: [], effort_metadata_version: 0 }]));
  await hydrateModelRegistry();
  expect(getProviders()[0].models[0]).toMatchObject({ effort_levels: canonical.effort_levels, default_effort: 'max', effort_metadata_version: 1, reasoning_capabilities: canonical });
 });
});

describe('bounded capability discovery recovery', () => {
 it('allows a legitimate 30-second catalog instead of aborting at 15 seconds', async () => {
  vi.useFakeTimers();
  mocks.post.mockClear();
  let finish!: (result: object) => void;
  mocks.post.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  mocks.get.mockResolvedValue(payload('slow', [{ id: 'm' }]));
  await hydrateModelRegistry();
  const signal = mocks.post.mock.calls[0][2].signal as AbortSignal;
  await vi.advanceTimersByTimeAsync(30_000);
  expect(signal.aborted).toBe(false);
  mocks.get.mockResolvedValue(payload('slow', [{ id: 'm', effort_metadata_version: 1, effort_levels: ['low'] }]));
  finish({ refreshed: ['slow'], failed: [] });
  await vi.advanceTimersByTimeAsync(0);
  expect(getProviders()[0].models[0].effort_levels).toEqual(['low']);
 });
 it('reconciles an abort, then retries with backoff rather than suppressing repair for the page', async () => {
  vi.useFakeTimers();
  mocks.post.mockClear().mockImplementation((_url, _body, options) => new Promise((_resolve, reject) => {
   options.signal.addEventListener('abort', () => reject(new Error('aborted')));
  }));
  mocks.get.mockResolvedValue(payload('retry-timeout', [{ id: 'm' }]));
  await hydrateModelRegistry();
  const reads = mocks.get.mock.calls.length;
  await vi.advanceTimersByTimeAsync(45_000);
  expect(mocks.get.mock.calls.length).toBe(reads + 1);
  await hydrateModelRegistry();
  expect(mocks.post).toHaveBeenCalledTimes(1);
  mocks.post.mockResolvedValue({ refreshed: ['retry-timeout'], failed: [] });
  await vi.advanceTimersByTimeAsync(59_999);
  expect(mocks.post).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(mocks.post).toHaveBeenCalledTimes(2);
 });
 it('queues batches of four with a fresh deadline for each request', async () => {
  vi.useFakeTimers();
  mocks.post.mockClear();
  const finishes: Array<() => void> = [];
  mocks.post.mockImplementation((_url, body) => new Promise(resolve => {
   finishes.push(() => resolve({ refreshed: body.provider_ids, failed: [] }));
  }));
  mocks.get.mockResolvedValue({ ...effortRegistryFixture, providers: Array.from({ length: 9 }, (_, i) => ({
   id: `batch-${i}`, name: 'P', provider_type: 'custom', models: [{ id: 'm' }]
  })) });
  await hydrateModelRegistry();
  expect(mocks.post).toHaveBeenCalledTimes(1);
  expect(mocks.post.mock.calls[0][1].provider_ids).toHaveLength(4);
  await vi.advanceTimersByTimeAsync(30_000);
  finishes[0]();
  await vi.advanceTimersByTimeAsync(0);
  expect(mocks.post).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(30_000);
  expect((mocks.post.mock.calls[1][2].signal as AbortSignal).aborted).toBe(false);
  finishes[1]();
  await vi.advanceTimersByTimeAsync(0);
  expect(mocks.post.mock.calls[2][1].provider_ids).toHaveLength(1);
  finishes[2]();
  await vi.advanceTimersByTimeAsync(0);
 });
 it('continues bounded backoff when the reconciliation read is also offline', async () => {
  vi.useFakeTimers();
  mocks.post.mockClear().mockRejectedValue(new Error('offline'));
  mocks.get.mockResolvedValue(payload('offline-read', [{ id: 'm' }]));
  await hydrateModelRegistry();
  await vi.advanceTimersByTimeAsync(0);
  mocks.get.mockRejectedValue(new Error('still offline'));
  await vi.advanceTimersByTimeAsync(60_000);
  expect(mocks.post).toHaveBeenCalledTimes(1);
  mocks.get.mockResolvedValue(payload('offline-read', [{ id: 'm' }]));
  mocks.post.mockResolvedValue({ refreshed: ['offline-read'], failed: [] });
  await vi.advanceTimersByTimeAsync(120_000);
  expect(mocks.post).toHaveBeenCalledTimes(2);
 });
 it('retries a changed provider revision despite prior successful unknown metadata', async () => {
  vi.useFakeTimers();
  mocks.post.mockClear().mockResolvedValue({ refreshed: ['changed-binding'], failed: [] });
  const p = payload('changed-binding', [{ id: 'm' }]);
  mocks.get.mockResolvedValue({ ...p, providers: [{ ...p.providers[0], capability_generation: 'old' }] });
  await hydrateModelRegistry();
  await vi.advanceTimersByTimeAsync(0);
  await hydrateModelRegistry();
  expect(mocks.post).toHaveBeenCalledTimes(1);
  mocks.get.mockResolvedValue({ ...p, providers: [{ ...p.providers[0], capability_generation: 'new' }] });
  await hydrateModelRegistry();
  await vi.advanceTimersByTimeAsync(0);
  expect(mocks.post).toHaveBeenCalledTimes(2);
 });
});
