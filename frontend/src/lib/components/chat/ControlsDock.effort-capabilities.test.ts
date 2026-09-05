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
import { effortRegistryFixture as payload } from '$lib/test-stubs/effort-registry';
const renderDock = () => render(ControlsDock, { props: { isUploading: false, onAttachClick: vi.fn() } });

describe('ControlsDock dynamic effort', () => {
 it('preserves XHigh on Astra after metadata hydration', async () => {
  activeSession.newChat();
  activeSession.setModelOverride('prov:or:openai/gpt-6-astra');
  activeSession.setEffortLevelOverride('xhigh');
  renderDock();
  await tick();
  expect(get(activeSession).effortLevelOverride).toBe('xhigh');
  mocks.get.mockResolvedValue(payload);
  await hydrateModelRegistry();
  await tick();
  expect(get(activeSession).effortLevelOverride).toBe('xhigh');
  await fireEvent.click(screen.getByRole('button', { name: 'Effort: XHigh' }));
  expect(screen.getByRole('slider').getAttribute('max')).toBe('4');
 });
 it('reconciles to the GLM default and renders six Sol and seven full options on mobile', async () => {
  mocks.get.mockResolvedValue(payload);
  await hydrateModelRegistry();
  activeSession.newChat();
  activeSession.setModelOverride('opus');
  activeSession.setEffortLevelOverride('xhigh');
  const { container } = renderDock();
  await fireEvent.click(screen.getByRole('button', { name: 'Effort: XHigh' }));
  activeSession.setModelOverride('prov:or:z-ai/glm-5.3-flash');
  await tick();
  expect(get(activeSession).effortLevelOverride).toBe('max');
  expect(screen.getByRole('slider').getAttribute('max')).toBe('2');
  await fireEvent.click(container.querySelector('.mobile-settings-trigger')!);
  expect(container.querySelectorAll('.effort-choices button')).toHaveLength(3);
  activeSession.setModelOverride('prov:or:openai/gpt-5.6-sol');
  await tick();
  expect(container.querySelectorAll('.effort-choices button')).toHaveLength(6);
  activeSession.setModelOverride('prov:or:seven');
  await tick();
  const choices = container.querySelectorAll<HTMLButtonElement>('.effort-choices button');
  expect(choices).toHaveLength(7);
  await fireEvent.click(choices[0]);
  expect(get(activeSession).effortLevelOverride).toBe('none');
  await fireEvent.click(choices[1]);
  expect(get(activeSession).effortLevelOverride).toBe('minimal');
  activeSession.setModelOverride('prov:or:single');
  await tick();
  expect(container.querySelectorAll('.effort-choices button')).toHaveLength(1);
  expect(get(activeSession).effortLevelOverride).toBe('high');
 });
 it('distinguishes unknown from unsupported on both surfaces without wiping unknown pins', async () => {
  mocks.get.mockResolvedValue(payload);
  await hydrateModelRegistry();
  activeSession.newChat();
  activeSession.setModelOverride('prov:or:unknown');
  activeSession.setEffortLevelOverride('minimal');
  const { container } = renderDock();
  await fireEvent.click(screen.getByRole('button', { name: 'Effort: minimal' }));
  expect(screen.queryByRole('slider')).toBeNull();
  expect(screen.getAllByText(/Reasoning effort capabilities are not known yet/)).toHaveLength(2);
  expect(get(activeSession).effortLevelOverride).toBe('minimal');
  await fireEvent.click(container.querySelector('.mobile-settings-trigger')!);
  expect(screen.getByText(/Reasoning effort capabilities are not known yet/)).toBeTruthy();
  expect(get(activeSession).effortLevelOverride).toBe('minimal');
  activeSession.setModelOverride('prov:or:unsupported');
  await tick();
  expect(get(activeSession).effortLevelOverride).toBeNull();
  expect(screen.getByText('Adjustable reasoning effort is not available for this model in AI Shuffle.')).toBeTruthy();
  expect(container.querySelectorAll('.effort-choices button')).toHaveLength(0);
 });
});
