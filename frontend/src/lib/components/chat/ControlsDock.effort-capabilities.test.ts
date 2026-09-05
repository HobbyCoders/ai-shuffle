import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import { activeSession } from '$lib/stores/active-session';
import { hydrateModelRegistry } from '$lib/constants/models';
import ControlsDock from './ControlsDock.svelte';
import '../../test-stubs/composer-dom';
const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.hoisted(() => {
 vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }));
});
vi.mock('$lib/api/client', async (importOriginal) => {
 const actual = await importOriginal<typeof import('$lib/api/client')>();
 return { ...actual, api: { ...actual.api, get: mocks.get },
  getUsageLimits: vi.fn().mockResolvedValue({ available: false, buckets: {}, provider_buckets: {} }) };
});
afterEach(() => cleanup());
const payload = {
 models: [{ value: 'opus', label: 'Opus', family: 'opus', effort_levels: ['low', 'medium', 'high', 'xhigh', 'max'] }],
 default: 'opus', legacy_map: {}, providers: [{ id: 'or', name: 'OpenRouter', provider_type: 'openrouter', models: [
  { id: 'gpt-6-astra', label: 'GPT-6 Astra', effort_levels: ['low', 'medium', 'high'] },
  { id: 'glm-5.3-flash', label: 'GLM 5.3 Flash', effort_levels: [] }
 ] }]
};
const renderDock = () => render(ControlsDock, { props: { isUploading: false, onAttachClick: vi.fn() } });

describe('ControlsDock dynamic effort', () => {
 it('preserves saved XHigh until provider metadata hydrates, then reconciles to High', async () => {
  activeSession.newChat();
  activeSession.setModelOverride('prov:or:gpt-6-astra');
  activeSession.setEffortLevelOverride('xhigh');
  renderDock();
  await tick();
  expect(get(activeSession).effortLevelOverride).toBe('xhigh');
  mocks.get.mockResolvedValue(payload);
  await hydrateModelRegistry();
  await tick();
  expect(get(activeSession).effortLevelOverride).toBe('high');
  await fireEvent.click(screen.getByRole('button', { name: 'Effort: High' }));
  expect(screen.getByRole('slider').getAttribute('max')).toBe('2');
  expect(screen.queryByText('XHigh')).toBeNull();
 });
 it('reconciles model switches and exposes the same no-effort explanation on desktop and mobile', async () => {
  activeSession.newChat();
  activeSession.setModelOverride('opus');
  activeSession.setEffortLevelOverride('xhigh');
  const { container } = renderDock();
  await fireEvent.click(screen.getByRole('button', { name: 'Effort: XHigh' }));
  expect(screen.getByRole('slider').getAttribute('max')).toBe('4');
  activeSession.setModelOverride('prov:or:glm-5.3-flash');
  await tick();
  expect(get(activeSession).effortLevelOverride).toBeNull();
  expect(screen.queryByRole('slider')).toBeNull();
  expect(screen.getByRole('dialog', { name: 'Reasoning effort' }).textContent).toContain('Adjustable reasoning effort is not available for this model in AI Shuffle.');
  await fireEvent.click(container.querySelector('.mobile-settings-trigger')!);
  expect(screen.getByText('Adjustable reasoning effort is not available for this model in AI Shuffle.')).toBeTruthy();
  expect(container.querySelectorAll('.effort-choices button')).toHaveLength(0);
  activeSession.setModelOverride('prov:or:gpt-6-astra');
  activeSession.setEffortLevelOverride('xhigh');
  await tick();
  expect(get(activeSession).effortLevelOverride).toBe('high');
  expect(container.querySelectorAll('.effort-choices button')).toHaveLength(3);
 });
});
