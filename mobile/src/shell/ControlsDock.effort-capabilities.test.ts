import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { composerPreferences } from '$lib/stores/composer-preferences';
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
beforeEach(() => composerPreferences.set('modelEfforts', {}));
afterEach(() => cleanup());
import { effortRegistryFixture as payload } from '$lib/test-stubs/effort-registry';
const renderDock = () => render(ControlsDock, { props: { isUploading: false, onAttachClick: vi.fn() } });
/** The Chat controls panel's effort bars (#1816), portaled to <body>. */
const panelEfforts = () => document.querySelectorAll<HTMLButtonElement>('.mcp .bars .bar');
const openPanel = () => fireEvent.click(screen.getByRole('button', { name: /^All chat controls/ }));
afterEach(() => document.querySelectorAll('.mcp-portal').forEach((n) => n.remove()));

describe('ControlsDock dynamic effort', () => {
 it.each(['desktop', 'mobile'].flatMap(surface => ['gpt-6-astra', 'gpt-6-luna', 'gpt-6.1-sol', 'gpt-6.1-terra'].map(model => [surface, model])))('preserves fast mode on %s when switching to %s and toggles both ways', async (surface, model) => {
  mocks.get.mockResolvedValue(payload);
  await hydrateModelRegistry();
  activeSession.newChat();
  activeSession.setModelOverride('prov:codex:gpt-5.6-sol');
  activeSession.setEffortLevelOverride('high');
  activeSession.setFastMode(true);
  const { container } = renderDock();
  await tick();
  activeSession.setModelOverride(`prov:codex:${model}`);
  await tick();
  expect(get(activeSession).fastMode).toBe(true);
  if (surface === 'desktop') {
   await fireEvent.click(screen.getByRole('button', { name: /^Effort:/ }));
  } else {
   await openPanel();
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
 it.each(['desktop', 'mobile'])('remembers fast mode per model on %s and restores it after an unsupported model', async (surface) => {
  mocks.get.mockResolvedValue(payload);
  await hydrateModelRegistry();
  for (const model of ['opus', 'prov:codex:gpt-6.1-sol']) composerPreferences.rememberFast(model, null);
  activeSession.newChat();
  activeSession.setModelOverride('opus');
  await activeSession.setFastMode(false);
  renderDock();
  await tick();
  const toggle = async () => {
   if (surface === 'desktop') await fireEvent.click(screen.getByRole('button', { name: /^Effort:/ }));
   else await openPanel();
   const control = screen.getByRole(surface === 'desktop' ? 'button' : 'switch', { name: 'Fast mode' });
   await fireEvent.click(control);
   await tick();
   cleanup(); document.querySelectorAll('.mcp-portal').forEach((n) => n.remove());
   renderDock();
   await tick();
  };
  await toggle();
  expect(get(activeSession).fastMode).toBe(true);
  activeSession.setModelOverride('prov:codex:gpt-6.1-sol');
  await tick();
  expect(get(activeSession).fastMode).toBe(true);
  await toggle();
  expect(get(activeSession).fastMode).toBe(false);
  activeSession.setModelOverride('prov:or:openai/gpt-6-astra');
  await tick();
  expect(get(activeSession).fastMode).toBe(false);
  activeSession.setModelOverride('opus');
  await tick();
  expect(get(activeSession).fastMode).toBe(true);
  activeSession.setModelOverride('prov:codex:gpt-6.1-sol');
  await tick();
  expect(get(activeSession).fastMode).toBe(false);
  expect(composerPreferences.fastForModel('opus')).toBe(true);
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
   await openPanel();
   const choices = panelEfforts();
   expect(Array.from(choices, el => el.textContent?.trim())).toEqual(['Low', 'Medium', 'High', 'XHigh', 'Max', 'Ultra']);
   await fireEvent.click(choices[5]);
  }
  expect(get(activeSession).effortLevelOverride).toBe('ultra');
  for (const id of ['gpt-6-astra', 'gpt-5.6-terra']) {
   activeSession.setModelOverride(`prov:codex:${id}`);
   await tick();
   expect(get(activeSession).effortLevelOverride).toBe('ultra');
  }
  activeSession.setModelOverride('prov:codex:gpt-5.6-sol');
  await tick();
  expect(get(activeSession).effortLevelOverride).toBe('ultra');
  activeSession.setModelOverride('prov:or:z-ai/glm-5.3-flash');
  await tick();
  expect(get(activeSession).effortLevelOverride).toBe('max');
  if (surface === 'desktop') {
   expect(screen.getByRole('slider').getAttribute('max')).toBe('2');
   expect(Array.from(container.querySelectorAll('.stops span'), el => el.textContent)).toEqual(['Low', 'High', 'Max']);
  } else {
   expect(Array.from(panelEfforts(), el => el.textContent?.trim())).toEqual(['Low', 'High', 'Max']);
  }
 });
 it.each(['desktop', 'mobile'])('restores independent model efforts through the rendered %s controls', async surface => {
  mocks.get.mockResolvedValue(payload);
  await hydrateModelRegistry();
  activeSession.newChat();
  activeSession.setModelOverride('opus');
  renderDock();
  if (surface === 'desktop') {
   await fireEvent.click(screen.getByRole('button', { name: /^Effort:/ }));
   await fireEvent.input(screen.getByRole('slider'), { target: { value: '0' } });
  } else {
   await openPanel(); await fireEvent.click(panelEfforts()[0]);
  }
  expect(get(activeSession).effortLevelOverride).toBe('low');
  activeSession.setModelOverride('prov:codex:gpt-5.6-sol'); await tick();
  expect(get(activeSession).effortLevelOverride).toBe('low');
  if (surface === 'desktop') await fireEvent.input(screen.getByRole('slider'), { target: { value: '3' } });
  else await fireEvent.click(panelEfforts()[3]);
  activeSession.setModelOverride('opus'); await tick();
  expect(get(activeSession).effortLevelOverride).toBe('low');
  if (surface === 'desktop') expect(screen.getByRole('slider').getAttribute('aria-valuetext')).toBe('Low');
  else expect(document.querySelector('.mcp .effort-tile .tile-value')?.textContent).toBe('Low');
  activeSession.setModelOverride('prov:codex:gpt-5.6-sol'); await tick();
  expect(get(activeSession).effortLevelOverride).toBe('xhigh');
  if (surface === 'desktop') expect(screen.getByRole('slider').getAttribute('aria-valuetext')).toBe('XHigh');
  else expect(document.querySelector('.mcp .effort-tile .tile-value')?.textContent).toBe('XHigh');
  activeSession.setModelOverride('prov:or:seven'); await tick();
  expect(get(activeSession).effortLevelOverride).toBe('xhigh');
  activeSession.setModelOverride('prov:or:z-ai/glm-5.3-flash'); await tick();
  expect(get(activeSession).effortLevelOverride).toBe('high');
 });
 it.each(['desktop', 'mobile'])('keeps concrete Opus versions independent through the %s controls', async surface => {
  const row=payload.providers.find(p=>p.id==='or')!;
  const template=row.models.find(m=>m.id==='seven')!;
  const versions=['claude-opus-4-6','claude-opus-4-7','claude-opus-4-8','claude-opus-5'];
  mocks.get.mockResolvedValue({...payload,providers:payload.providers.map(p=>p.id==='or'
   ? {...p,models:[...p.models,...versions.map(id=>({...template,id,label:id}))]} : p)});
  await hydrateModelRegistry(); activeSession.newChat();
  activeSession.setModelOverride('prov:or:claude-opus-4-6'); renderDock();
  if (surface==='desktop') await fireEvent.click(screen.getByRole('button',{name:/^Effort:/}));
  else await openPanel();
  const pick=async(index:number)=> surface==='desktop'
   ? fireEvent.input(screen.getByRole('slider'),{target:{value:String(index)}})
   : fireEvent.click(panelEfforts()[index]);
  for (const [index,id] of versions.entries()) {
   activeSession.setModelOverride('prov:or:'+id); await tick();
   await pick([2,3,4,6][index]);
  }
  for (const [index,id] of versions.entries()) {
   activeSession.setModelOverride('prov:or:'+id); await tick();
   const label=['Low','Medium','High','Max'][index];
   expect(get(activeSession).effortLevelOverride).toBe(label.toLowerCase());
   if(surface==='desktop') expect(screen.getByRole('slider').getAttribute('aria-valuetext')).toBe(label);
   else expect(document.querySelector('.mcp .effort-tile .tile-value')?.textContent).toBe(label);
  }
  expect(composerPreferences.effortForModel('prov:api:claude-opus-4-6-20260205[1m]')).toBe('low');
  expect(composerPreferences.effortForModel('opus')).toBe('max');
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
 it('clamps a GLM pick to the nearest level and renders six Sol and seven full options on mobile', async () => {
  mocks.get.mockResolvedValue(payload);
  await hydrateModelRegistry();
  activeSession.newChat();
  activeSession.setModelOverride('opus');
  activeSession.setEffortLevelOverride('xhigh');
  const { container } = renderDock();
  await fireEvent.click(screen.getByRole('button', { name: 'Effort: XHigh' }));
  activeSession.setModelOverride('prov:or:z-ai/glm-5.3-flash');
  await tick();
  // High and Max are equally close; the shared policy prefers the lower level.
  expect(get(activeSession).effortLevelOverride).toBe('high');
  expect(screen.getByRole('slider').getAttribute('max')).toBe('2');
  await openPanel();
  expect(panelEfforts()).toHaveLength(3);
  activeSession.setModelOverride('prov:or:openai/gpt-5.6-sol');
  await tick();
  expect(panelEfforts()).toHaveLength(6);
  activeSession.setModelOverride('prov:or:seven');
  await tick();
  const choices = panelEfforts();
  expect(choices).toHaveLength(7);
  await fireEvent.click(choices[0]);
  expect(get(activeSession).effortLevelOverride).toBe('none');
  await fireEvent.click(panelEfforts()[1]);
  expect(get(activeSession).effortLevelOverride).toBe('minimal');
  activeSession.setModelOverride('prov:or:single');
  activeSession.setEffortLevelOverride('minimal');
  await tick();
  expect(panelEfforts()).toHaveLength(1);
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
  expect(screen.getAllByText(/Reasoning effort capabilities are not known yet/)).toHaveLength(1);
  expect(get(activeSession).effortLevelOverride).toBe('minimal');
  // The phone keeps the unknown pin visible and offers no levels to pick.
  expect(screen.getByRole('button', { name: 'Reasoning effort: minimal' })).toBeTruthy();
  await openPanel();
  expect(document.querySelector('.mcp .effort-tile .tile-value')?.textContent).toBe('minimal');
  expect(panelEfforts()).toHaveLength(0);
  expect(get(activeSession).effortLevelOverride).toBe('minimal');
  activeSession.setModelOverride('prov:or:unsupported');
  await tick();
  expect(get(activeSession).effortLevelOverride).toBeNull();
  // Opening the panel closed the desktop popover that showed the long explanation.
  expect(screen.getByRole('button', { name: 'Reasoning effort: set by the model' })).toBeTruthy();
  expect(document.querySelector('.mcp .effort-tile .tile-value')?.textContent).toBe('Set by the model');
  expect(panelEfforts()).toHaveLength(0);
 });
});
