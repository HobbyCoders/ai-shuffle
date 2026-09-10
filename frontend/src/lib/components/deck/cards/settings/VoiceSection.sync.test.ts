import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
const identity = vi.hoisted(() => ({ epoch: 0, listeners: new Set<() => void>() }));
vi.mock('$lib/api/client', () => ({ api: transport }));
vi.mock('$lib/stores/session-owner', () => ({
 sessionOwnerEpoch: () => identity.epoch,
 sessionOwnerBoundary: { subscribe(callback: () => void) {
  identity.listeners.add(callback); callback(); return () => identity.listeners.delete(callback);
 } }
}));
import VoiceSection from './VoiceSection.svelte';
import { resourceSync } from '$lib/services/resource-sync';

const settings = (voice = 'marin', minutes = 60) => ({ voice_mode_enabled: true,
 voice_model: 'gpt-realtime-2.1', voice_voice: voice, voice_provider_id: null,
 voice_max_call_minutes: minutes, voice_app_tools_enabled: false });
function changeAccount() {
 identity.epoch++;
 for (const listener of identity.listeners) listener();
}
beforeEach(() => { transport.get.mockReset(); transport.put.mockReset(); changeAccount(); });
afterEach(() => { cleanup(); resourceSync.clear(); });

async function mountSection(appToolsEnabled = false) {
 let server = { ...settings(), voice_app_tools_enabled: appToolsEnabled };
 transport.get.mockImplementation(async (path: string) => {
  if (path === '/settings/voice') return {...server};
  if (path === '/voice/providers') return [{ id: 'openai-key', name: 'OpenAI API key' }];
  if (path === '/voice/agents') return [{ id: 'research', name: 'Research agent', description: 'Research tasks', model: 'gpt-5', provider_name: 'OpenAI', provider_id: 'openai-key' }];
  return [];
 });
 const view = render(VoiceSection);
 await screen.findByLabelText('Agent voice');
 return { ...view, remote(value: ReturnType<typeof settings>) {
  server = value; resourceSync.invalidate(['settings']);
 } };
}

describe('voice settings controls', () => {
 it('offers GPT Live 1 without a fal.ai limitation notice', async () => {
  await mountSection();
  expect(screen.getByRole('option', { name: 'GPT Live 1' }).getAttribute('value')).toBe('gpt-live-1');
  expect(screen.queryByText(/not integrated with voice calls yet/)).toBeNull();
 });

 it.each([false, true])('omits the entire voice-agent capabilities group and roster when the legacy API flag is %s', async (enabled) => {
  await mountSection(enabled);
  await screen.findByLabelText('Voice provider');
  expect(screen.queryByTestId('voice-agent-section')).toBeNull();
  expect(screen.queryByText('What the voice agent can do')).toBeNull();
  expect(screen.queryByRole('switch', { name: 'Manage app configuration by voice' })).toBeNull();
  expect(screen.queryByText('Agents available in calls')).toBeNull();
  expect(screen.queryByText('Research agent')).toBeNull();
 });

 it('loads settings and providers without fetching a voice-agent roster, including after refresh and remount', async () => {
  const view = await mountSection();
  await screen.findByLabelText('Voice provider');
  expect(transport.get.mock.calls.map(([path]) => path)).toEqual(['/settings/voice', '/voice/providers']);
  view.remote(settings('cedar', 90));
  await waitFor(() => expect((screen.getByLabelText('Agent voice') as HTMLSelectElement).value).toBe('cedar'));
  view.unmount();
  render(VoiceSection);
  await screen.findByLabelText('Voice provider');
  expect(transport.get.mock.calls.map(([path]) => path)).not.toContain('/voice/agents');
  expect(transport.put).not.toHaveBeenCalled();
 });

 it('preserves the remaining controls and saves only the setting being edited', async () => {
  await mountSection();
  await screen.findByLabelText('Voice provider');
  expect(screen.getByRole('switch', { name: 'Enable voice mode' }).getAttribute('aria-checked')).toBe('true');
  expect((screen.getByLabelText('Realtime model') as HTMLSelectElement).value).toBe('gpt-realtime-2.1');
  expect((screen.getByLabelText('Agent voice') as HTMLSelectElement).value).toBe('marin');
  expect(screen.getByRole('button', { name: 'Preview the selected voice' })).toBeTruthy();
  expect((screen.getByLabelText('Max call minutes') as HTMLInputElement).value).toBe('60');
  transport.put.mockResolvedValue({ ...settings(), voice_max_call_minutes: 120 });
  await fireEvent.input(screen.getByLabelText('Max call minutes'), {target:{value:'120'}});
  await fireEvent.blur(screen.getByLabelText('Max call minutes'));
  expect(transport.put).toHaveBeenCalledExactlyOnceWith('/settings/voice', { voice_max_call_minutes: 120 });
 });
});

describe('remaining voice settings writes', () => {
 it.each([
  ['Voice provider', 'openai-key', { voice_provider_id: 'openai-key' }],
  ['Voice provider', '', { voice_provider_id: null }],
  ['Realtime model', 'gpt-live-1', { voice_model: 'gpt-live-1' }],
  ['Realtime model', 'gpt-realtime-2.1-mini', { voice_model: 'gpt-realtime-2.1-mini' }],
  ['Agent voice', 'cedar', { voice_voice: 'cedar' }],
 ] as const)('saves %s as %s without writing the removed capability flag', async (label, value, patch) => {
  await mountSection();
  await screen.findByLabelText('Voice provider');
  transport.put.mockResolvedValue({ ...settings(), ...patch });
  await fireEvent.change(screen.getByLabelText(label), { target: { value } });
  expect(transport.put).toHaveBeenCalledExactlyOnceWith('/settings/voice', patch);
 });

 it('still changes whether voice mode is enabled without writing the removed capability flag', async () => {
  await mountSection();
  transport.put.mockResolvedValue({ ...settings(), voice_mode_enabled: false });
  await fireEvent.click(screen.getByRole('switch', { name: 'Enable voice mode' }));
  expect(transport.put).toHaveBeenCalledExactlyOnceWith('/settings/voice', { voice_mode_enabled: false });
 });

 it('keeps the provider setup pointer and voice controls when eligible providers cannot load', async () => {
  const failure = new Error('Providers unavailable');
  const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
  try {
   transport.get.mockImplementation(async (path: string) => {
    if (path === '/settings/voice') return settings();
    throw failure;
   });
   render(VoiceSection);
   await screen.findByLabelText('Agent voice');
   await waitFor(() => expect(warning).toHaveBeenCalledWith('[voice] could not list eligible providers', failure));
   expect(screen.getByText(/Add an OpenAI provider or a fal.ai API key/)).toBeTruthy();
   expect(screen.getByRole('button', { name: 'Preview the selected voice' })).toBeTruthy();
   expect(transport.get.mock.calls.map(([path]) => path)).toEqual(['/settings/voice', '/voice/providers']);
  } finally {
   warning.mockRestore();
  }
 });
});

describe('mounted voice settings synchronization', () => {
 it('refreshes controls without sending settings writes', async () => {
  const view = await mountSection();
  view.remote(settings('cedar', 90));
  await waitFor(() => expect((screen.getByLabelText('Agent voice') as HTMLSelectElement).value).toBe('cedar'));
  expect((screen.getByLabelText('Max call minutes') as HTMLInputElement).value).toBe('90');
  expect(transport.put).not.toHaveBeenCalled();
 });

 it('keeps an uncommitted minutes field while other controls refresh', async () => {
  const view = await mountSection();
  await fireEvent.input(screen.getByLabelText('Max call minutes'), {target:{value:'123'}});
  view.remote(settings('cedar', 90));
  await waitFor(() => expect((screen.getByLabelText('Agent voice') as HTMLSelectElement).value).toBe('cedar'));
  expect((screen.getByLabelText('Max call minutes') as HTMLInputElement).value).toBe('123');
  expect(transport.put).not.toHaveBeenCalled();
 });

 it('prevents an old editor from saving after an account change', async () => {
  await mountSection();
  await fireEvent.input(screen.getByLabelText('Max call minutes'), {target:{value:'123'}});
  changeAccount();
  await fireEvent.blur(screen.getByLabelText('Max call minutes'));
  expect(transport.put).not.toHaveBeenCalled();
 });
});
