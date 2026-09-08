import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import { activeSession } from '$lib/stores/active-session';
import { hydrateModelRegistry } from '$lib/constants/models';
import ControlsDock from '$lib/components/chat/ControlsDock.svelte';
import '$lib/test-stubs/composer-dom';
import { installMobileShell } from './register';

// Both surfaces render here; the mobile one needs the registered controls sheet.
installMobileShell();
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
 it.each(['desktop', 'mobile'])('preserves fast mode when switching to Astra and lets it toggle both ways on %s', async (surface) => {
  mocks.get.mockResolvedValue(payload);
  await hydrateModelRegistry();
  activeSession.newChat();
  activeSession.setModelOverride('prov:codex:gpt-5.6-sol');
  activeSession.setEffortLevelOverride('high');
  activeSession.setFastMode(true);
  const { container } = renderDock();
  await tick();
  activeSession.setModelOverride('prov:codex:gpt-6-astra');
  await tick();
  expect(get(activeSession).fastMode).toBe(true);
  if (surface === 'desktop') {
   await fireEvent.click(screen.getByRole('button', { name: 'Effort: High' }));
  } else {
   await fireEvent.click(container.querySelector('.mobile-settings-trigger')!);
  }
  const toggle = screen.getByRole(surface === 'desktop' ? 'button' : 'switch', { name: 'Fast mode' });
  const stateAttribute = surface === 'desktop' ? 'aria-pressed' : 'aria-checked';
  expect(toggle.getAttribute(stateAttribute)).toBe('true');
  await fireEvent.click(toggle);
  expect(get(activeSession).fastMode).toBe(false);
  expect(toggle.getAttribute(stateAttribute)).toBe('false');
  await fireEvent.click(toggle);
  expect(get(activeSession).fastMode).toBe(true);
  expect(toggle.getAttribute(stateAttribute)).toBe('true');
  expect(get(activeSession).effortLevelOverride).toBe('high');
  activeSession.setModelOverride('prov:or:openai/gpt-6-astra');
  await tick();
  expect(get(activeSession).fastMode).toBe(false);
  expect(screen.queryByRole(surface === 'desktop' ? 'button' : 'switch', { name: 'Fast mode' })).toBeNull();
 });
 it.each(['desktop', 'mobile'])('offers six live Codex levels and no Ultra for sparse GLM on %s', async (surface) => {
  mocks.get.mockResolvedValue(payload);
  await hydrateModelRegistry();
  activeSession.newChat();
  activeSession.setModelOverride('prov:codex:gpt-5.6-sol');
  activeSession.setEffortLevelOverride('none');
  const { container } = renderDock();
  await tick();
  expect(get(activeSession).effortLevelOverride).toBe('low');
  if (surface === 'desktop') {
   await fireEvent.click(screen.getByRole('button', { name: 'Effort: Low' }));
   expect(screen.getByRole('slider').getAttribute('max')).toBe('5');
   expect(Array.from(container.querySelectorAll('.stops span'), el => el.textContent)).toEqual(['Low', 'Medium', 'High', 'XHigh', 'Max', 'Ultra']);
   await fireEvent.input(screen.getByRole('slider'), { target: { value: '5' } });
  } else {
   await fireEvent.click(container.querySelector('.mobile-settings-trigger')!);
   const choices = container.querySelectorAll<HTMLButtonElement>('.effort-choices button');
   expect(Array.from(choices, el => el.textContent?.trim())).toEqual(['Low', 'Medium', 'High', 'XHigh', 'Max', 'Ultra']);
   await fireEvent.click(choices[5]);
  }
  expect(get(activeSession).effortLevelOverride).toBe('ultra');
  for (const id of ['gpt-6-astra', 'gpt-5.6-terra']) {
   activeSession.setModelOverride(`prov:codex:${id}`);
   await tick();
   expect(get(activeSession).effortLevelOverride).toBe('ultra');
  }
  activeSession.setModelOverride('prov:or:z-ai/glm-5.3-flash');
  await tick();
  expect(get(activeSession).effortLevelOverride).toBe('max');
  if (surface === 'desktop') {
   expect(screen.getByRole('slider').getAttribute('max')).toBe('2');
   expect(Array.from(container.querySelectorAll('.stops span'), el => el.textContent)).toEqual(['Low', 'High', 'Max']);
  } else {
   expect(Array.from(container.querySelectorAll('.effort-choices button'), el => el.textContent?.trim())).toEqual(['Low', 'High', 'Max']);
  }
 });
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
