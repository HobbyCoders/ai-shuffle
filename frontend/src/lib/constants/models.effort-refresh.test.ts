import { describe, it, expect, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: mocks }));
import { hydrateModelRegistry, getProviders, isRegistryHydrated } from './models';
import { effortRegistryFixture } from '$lib/test-stubs/effort-registry';
const payload = (id: string, models: object[]) => ({ ...effortRegistryFixture, providers: [{ id, name: id, provider_type: 'openrouter', models }] });

describe('capability migration hydration', () => {
 it('makes the initial registry ready without waiting and refreshes each stale provider once', async () => {
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
 it('retains state on failure, does not retry automatically, and accepts an explicit later hydration', async () => {
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
