import { beforeEach, describe, it, expect, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: mocks }));
import { hydrateModelRegistry } from './models';
import { resolveEffortModel, resolveModelEffort, reconcileModelEffort, modelEffortOptions, UNKNOWN_EFFORT, NO_ADJUSTABLE_EFFORT } from './model-effort';
import { effortRegistryFixture } from '$lib/test-stubs/effort-registry';
const levels = (model: string) => resolveModelEffort(model).options.map(o => o.value);
const capability = (id: string) => resolveModelEffort(`prov:or:${id}`);

beforeEach(async () => {
 mocks.get.mockResolvedValue(effortRegistryFixture);
 await hydrateModelRegistry();
});

describe('authoritative effort capabilities', () => {
 it('uses live Codex levels and defaults without adding Ultra to Luna or OpenRouter GLM', () => {
  for (const id of ['gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra']) {
   const caps = resolveModelEffort(`prov:codex:${id}`);
   expect(caps.options.map(o => o.value)).toEqual(['low', 'medium', 'high', 'xhigh', 'max', 'ultra']);
   expect(reconcileModelEffort('ultra', caps)).toBe('ultra');
  }
  const sol = resolveModelEffort('prov:codex:gpt-5.6-sol');
  expect(sol.defaultValue).toBe('low');
  expect(reconcileModelEffort('none', sol)).toBe('low');
  expect(reconcileModelEffort(null, sol)).toBeNull();
  expect(resolveModelEffort('prov:codex:gpt-6-astra').defaultValue).toBe('medium');
  expect(levels('prov:codex:gpt-5.6-luna')).toEqual(['low', 'medium', 'high', 'xhigh', 'max']);
  expect(levels('prov:or:z-ai/glm-5.3-flash')).toEqual(['low', 'high', 'max']);
  expect(reconcileModelEffort('ultra', capability('z-ai/glm-5.3-flash'))).toBe('max');
 });
 it('uses exact documented Astra, Sol and sparse GLM levels, not family guesses', () => {
  expect(levels('prov:or:openai/gpt-6-astra')).toEqual(['low', 'medium', 'high', 'xhigh', 'max']);
  expect(levels('prov:or:openai/gpt-5.6-sol')).toEqual(['none', 'low', 'medium', 'high', 'xhigh', 'max']);
  expect(levels('prov:or:z-ai/glm-5.3-flash')).toEqual(['low', 'high', 'max']);
  expect(capability('gpt-6-astra-custom').known).toBe(false);
 });
 it('distinguishes unknown and unsupported and preserves unknown pins', () => {
  expect(capability('unknown').explanation).toBe(UNKNOWN_EFFORT);
  expect(capability('unsupported').explanation).toBe(NO_ADJUSTABLE_EFFORT);
  expect(reconcileModelEffort('xhigh', capability('unknown'))).toBe('xhigh');
  expect(reconcileModelEffort('xhigh', capability('missing'))).toBe('xhigh');
  expect(reconcileModelEffort('xhigh', capability('unsupported'))).toBeNull();
 });
 it('clamps to nearest supported levels without confusing literal none with omission', () => {
  expect(reconcileModelEffort('xhigh', capability('z-ai/glm-5.3-flash'))).toBe('high');
  expect(reconcileModelEffort('minimal', capability('openai/gpt-5.6-sol'))).toBe('none');
  expect(reconcileModelEffort('none', capability('openai/gpt-5.6-sol'))).toBe('none');
  expect(reconcileModelEffort(null, capability('openai/gpt-5.6-sol'))).toBeNull();
  expect(reconcileModelEffort('low', capability('single'))).toBe('high');
 });
 it('accepts seven options and backend mandatory filtering without synthesizing none', () => {
  expect(levels('seven')).toEqual(['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']);
  expect(levels('mandatory')).toEqual(['low', 'high']);
  expect(reconcileModelEffort('none', capability('mandatory'))).toBe('low');
 });
 it('preserves unknown settings pins but does not advertise OpenAI-only levels for native models', () => {
  expect(modelEffortOptions('prov:or:unknown', 'minimal').map(o => o.value)).toEqual(['minimal']);
  expect(modelEffortOptions('opus', null).map(o => o.value)).not.toContain('none');
  expect(modelEffortOptions('opus', null).map(o => o.value)).not.toContain('minimal');
 });
 it('treats absent and null metadata as unknown', async () => {
  mocks.post.mockResolvedValue({ refreshed: [], failed: ['old'] });
  mocks.get.mockResolvedValue({ ...effortRegistryFixture, providers: [{ id: 'old', models: [
   { id: 'absent' }, { id: 'stale', effort_levels: null }
  ] }] });
  await hydrateModelRegistry();
  for (const id of ['absent', 'stale']) {
   const caps = resolveModelEffort(`prov:old:${id}`);
   expect(caps.known).toBe(false);
   expect(reconcileModelEffort('none', caps)).toBe('none');
  }
 });
 it('sources native fallback from explicit registry entries and respects declared unknown', async () => {
  mocks.get.mockResolvedValue({ models: [
   { value: 'sonnet', label: 'Sonnet', family: 'sonnet' },
   { value: 'opus', label: 'Opus', family: 'opus', effort_levels: null, default_effort: null }
  ], default: 'sonnet', providers: [] });
  await hydrateModelRegistry();
  expect(levels('sonnet')).toEqual(['low', 'medium', 'high', 'xhigh', 'max']);
  expect(resolveModelEffort('opus').known).toBe(false);
  expect(reconcileModelEffort('max', resolveModelEffort('opus'))).toBe('max');
 });
 it('resolves inherited defaults, provider routing and current native reroutes, not stale live turns', () => {
  expect(resolveEffortModel(null, null, null, null)).toBe('opus');
  expect(resolveEffortModel('opus', 'haiku', 'opus', null)).toBe('haiku');
  expect(resolveEffortModel('sonnet', 'haiku', 'opus', null)).toBe('sonnet');
  expect(resolveEffortModel('glm', 'glm', 'glm', 'or')).toBe('prov:or:glm');
  expect(resolveEffortModel('prov:other:glm', 'glm', 'glm', 'or')).toBe('prov:other:glm');
 });
});
