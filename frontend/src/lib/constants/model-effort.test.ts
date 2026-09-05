import { describe, it, expect, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get } }));
import { hydrateModelRegistry } from './models';
import { resolveEffortModel, resolveModelEffort, reconcileModelEffort } from './model-effort';
const levels = (model: string) => resolveModelEffort(model).options.map(o => o.value);

describe('model effort capabilities', () => {
 it('preserves saved effort before registry hydration', () => {
  expect(reconcileModelEffort('xhigh', resolveModelEffort('prov:or:z-ai/glm-5.3-flash'))).toBe('xhigh');
 });
 it('uses conservative, model-dependent fallback for old payloads', async () => {
  mocks.get.mockResolvedValue({ models: [
   { value: 'sonnet', label: 'Sonnet', family: 'sonnet' },
   { value: 'opus', label: 'Opus', family: 'opus' }
  ], default: 'sonnet', legacy_map: {}, providers: [] });
  await hydrateModelRegistry();
  expect(levels('sonnet')).toEqual(['low', 'medium', 'high']);
  expect(levels('opus')).toEqual(['low', 'medium', 'high', 'max']);
  for (const model of ['haiku', 'fable', 'unknown', 'prov:or:z-ai/glm-5.3-flash']) expect(levels(model)).toEqual([]);
  for (const provider of ['or', 'openai', 'responses']) {
   const model = `prov:${provider}:openai/gpt-6-astra`;
   expect(levels(model)).toEqual([]);
   expect(reconcileModelEffort('xhigh', resolveModelEffort(model))).toBeNull();
  }
 });
 it('honors effective metadata, explicit empty arrays and supported defaults after hydration', async () => {
  mocks.get.mockResolvedValue({ models: [
   { value: 'opus', label: 'Opus', family: 'opus', effort_levels: ['low', 'high', 'xhigh'], default_effort: 'xhigh' }
  ], default: 'opus', legacy_map: {}, providers: [{ id: 'or', name: 'OpenRouter', provider_type: 'openrouter',
   models: [
    { id: 'glm', label: 'GLM', effort_levels: [] },
    { id: 'reasoner', label: 'Reasoner', effort_levels: ['low', 'medium', 'high'], default_effort: 'medium' },
    { id: 'single', label: 'Single', effort_levels: ['low'], default_effort: 'max' },
    { id: 'invalid', label: 'Invalid', effort_levels: ['unknown'] }
   ] }] });
  await hydrateModelRegistry();
  expect(levels('opus')).toEqual(['low', 'high', 'xhigh']);
  expect(levels('reasoner')).toEqual(['low', 'medium', 'high']);
  expect(reconcileModelEffort('max', resolveModelEffort('opus'))).toBe('xhigh');
  expect(reconcileModelEffort('xhigh', resolveModelEffort('prov:or:glm'))).toBeNull();
  expect(reconcileModelEffort('xhigh', resolveModelEffort('prov:or:reasoner'))).toBe('medium');
  expect(reconcileModelEffort(null, resolveModelEffort('prov:or:reasoner'))).toBeNull();
  expect(reconcileModelEffort('high', resolveModelEffort('prov:or:single'))).toBe('low');
  expect(levels('prov:or:invalid')).toEqual([]);
 });
 it('resolves inherited defaults, provider routing and current native reroutes, not stale live turns', () => {
  expect(resolveEffortModel(null, null, null, null)).toBe('opus');
  expect(resolveEffortModel('opus', 'haiku', 'opus', null)).toBe('haiku');
  expect(resolveEffortModel('sonnet', 'haiku', 'opus', null)).toBe('sonnet');
  expect(resolveEffortModel('glm', 'glm', 'glm', 'or')).toBe('prov:or:glm');
  expect(resolveEffortModel('prov:other:glm', 'glm', 'glm', 'or')).toBe('prov:other:glm');
 });
});
