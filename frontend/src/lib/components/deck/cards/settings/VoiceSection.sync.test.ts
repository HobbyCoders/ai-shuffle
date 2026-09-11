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
 voice_model: 'gpt-live-1', voice_voice: voice, voice_provider_id: null,
 voice_max_call_minutes: minutes, voice_app_tools_enabled: false,
 model_details: { model: 'gpt-live-1', transport: 'stream' as const,
  voices: ['marin', 'cedar'].map(id => ({id, label: id})), runtime_settings: [],
  prompts: [{label: 'Voice agent', text: 'Actual server prompt'}], prompt_note: 'Runtime context is appended.',
  preview_note: 'Start a call to hear this model accurately.' } });
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
 await screen.findByRole('radio', {name:'marin'});
 return { ...view, remote(value: ReturnType<typeof settings>) {
  server = value; resourceSync.invalidate(['settings']);
 } };
}



describe('GPT Live voice settings', () => {
 it('defaults to Auto, saves the timeout, and hides it in Toggle mode', async () => {
  const view = await mountSection();
  expect((screen.getByLabelText('Silence timeout (seconds)') as HTMLInputElement).value).toBe('5');
  transport.put.mockResolvedValue({...settings(), voice_input_silence_seconds: 9});
  await fireEvent.input(screen.getByLabelText('Silence timeout (seconds)'), {target:{value:'9'}});
  await fireEvent.blur(screen.getByLabelText('Silence timeout (seconds)'));
  await waitFor(() => expect(transport.put).toHaveBeenCalledWith('/settings/voice', {voice_input_silence_seconds:9}));
  await waitFor(() => expect((screen.getByLabelText('Composer microphone mode') as HTMLSelectElement).disabled).toBe(false));
  transport.put.mockResolvedValue({...settings(),voice_input_mode:'toggle'});
  await fireEvent.change(screen.getByLabelText('Composer microphone mode'), {target:{value:'toggle'}});
  await waitFor(() => expect(transport.put).toHaveBeenCalledWith('/settings/voice', {voice_input_mode:'toggle'}));
  view.remote({...settings(), voice_input_mode:'toggle'} as ReturnType<typeof settings>);
  await waitFor(() => expect(screen.queryByLabelText('Silence timeout (seconds)')).toBeNull());
 });
 it.each(['', '0', '61', '2.5'])('rejects invalid silence timeout %s without saving', async value => {
  await mountSection();
  await fireEvent.input(screen.getByLabelText('Silence timeout (seconds)'), {target:{value}});
  await fireEvent.blur(screen.getByLabelText('Silence timeout (seconds)'));
  expect(transport.put).not.toHaveBeenCalled();
  expect(screen.getByText('Silence timeout must be a whole number from 1 to 60 seconds.')).toBeTruthy();
 });
 it('shows a play button beside every voice and no provider or model selector', async () => {
  await mountSection();
  expect(screen.getByRole('button', {name:'Play marin voice preview'})).toBeTruthy();
  expect(screen.getByRole('button', {name:'Play cedar voice preview'})).toBeTruthy();
  expect(screen.getAllByRole('combobox')).toHaveLength(1);
  expect(screen.getByRole('combobox', {name:'Composer microphone mode'})).toBeTruthy();
  expect(transport.get.mock.calls.map(([path]) => path)).toEqual(['/settings/voice']);
 });
 it('loads new server voices without hardcoding their names', async () => {
  const view=await mountSection();
  view.remote({...settings('quartz'), model_details:{...settings().model_details, voices:[{id:'quartz',label:'Quartz'}]}});
  await screen.findByRole('radio', {name:'Quartz'});
  expect(screen.getByRole('button', {name:'Play Quartz voice preview'})).toBeTruthy();
 });
 it('saves voice selection without changing the model, provider or capability settings', async () => {
  await mountSection();
  transport.put.mockResolvedValue(settings('cedar'));
  await fireEvent.click(screen.getByRole('radio', {name:'cedar'}));
  expect(transport.put).toHaveBeenCalledExactlyOnceWith('/settings/voice', {voice_voice:'cedar'});
 });
 it('saves only the edited call limit', async () => {
  await mountSection();
  transport.put.mockResolvedValue(settings('marin',120));
  await fireEvent.input(screen.getByLabelText('Max call minutes'), {target:{value:'120'}});
  await fireEvent.blur(screen.getByLabelText('Max call minutes'));
  expect(transport.put).toHaveBeenCalledExactlyOnceWith('/settings/voice', {voice_max_call_minutes:120});
 });
 it('still changes whether voice mode is enabled', async () => {
  await mountSection();
  transport.put.mockResolvedValue({...settings(),voice_mode_enabled:false});
  await fireEvent.click(screen.getByRole('switch', {name:'Enable voice mode'}));
  expect(transport.put).toHaveBeenCalledExactlyOnceWith('/settings/voice', {voice_mode_enabled:false});
 });
});

describe('mounted voice settings synchronization', () => {
 it('refreshes controls without sending settings writes', async () => {
  const view = await mountSection();
  view.remote(settings('cedar', 90));
  await waitFor(() => expect((screen.getByRole('radio', {name:'cedar'}) as HTMLInputElement).checked).toBe(true));
  expect((screen.getByLabelText('Max call minutes') as HTMLInputElement).value).toBe('90');
  expect(transport.put).not.toHaveBeenCalled();
 });

 it('keeps an uncommitted minutes field while other controls refresh', async () => {
  const view = await mountSection();
  await fireEvent.input(screen.getByLabelText('Max call minutes'), {target:{value:'123'}});
  view.remote(settings('cedar', 90));
  await waitFor(() => expect((screen.getByRole('radio', {name:'cedar'}) as HTMLInputElement).checked).toBe(true));
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

describe('voice preview playback', () => {
 it('plays the requested voice without changing selection and reuses its sample', async () => {
  const module = await import('$lib/api/voice');
  const preview = vi.spyOn(module, 'previewVoice').mockResolvedValue(new Blob(['audio'], {type:'audio/wav'}));
  const play = vi.fn().mockResolvedValue(undefined), pause = vi.fn();
  vi.stubGlobal('Audio', class {src='';onended=null;onerror=null;play=play;pause=pause;});
  const create=vi.spyOn(URL,'createObjectURL').mockReturnValue('blob:sample');
  const revoke=vi.spyOn(URL,'revokeObjectURL').mockImplementation(()=>{});
  try {
   const view=await mountSection();
   await fireEvent.click(screen.getByRole('button', {name:'Play cedar voice preview'}));
   await waitFor(()=>expect(play).toHaveBeenCalledOnce());
   expect(preview).toHaveBeenCalledWith('cedar',expect.any(AbortSignal));
   expect(transport.put).not.toHaveBeenCalled();
   await fireEvent.click(screen.getByRole('button', {name:'Stop preview of cedar'}));
   expect(pause).toHaveBeenCalledOnce();
   await fireEvent.click(screen.getByRole('button', {name:'Play cedar voice preview'}));
   await waitFor(()=>expect(play).toHaveBeenCalledTimes(2));
   expect(preview).toHaveBeenCalledOnce();
   view.unmount();
   expect(revoke).toHaveBeenCalledWith('blob:sample');
  } finally {preview.mockRestore();create.mockRestore();revoke.mockRestore();vi.unstubAllGlobals();}
 });
 it('aborts a pending sample on stop and reports failed previews accessibly', async () => {
  const module = await import('$lib/api/voice');
  let signal: AbortSignal | undefined;
  const preview = vi.spyOn(module,'previewVoice').mockImplementation((_voice,abort) => {
   signal=abort;
   return new Promise((_resolve,reject)=>abort?.addEventListener('abort',()=>reject(new DOMException('Cancelled','AbortError'))));
  });
  vi.stubGlobal('Audio', class {pause() {}});
  try {
   await mountSection();
   await fireEvent.click(screen.getByRole('button',{name:'Play cedar voice preview'}));
   expect(screen.getByRole('status').textContent).toContain('Generating');
   await fireEvent.click(screen.getByRole('button',{name:'Stop preview of cedar'}));
   expect(signal?.aborted).toBe(true);
   expect(screen.queryByRole('alert')).toBeNull();
   preview.mockRejectedValue({detail:'GPT Live unavailable'});
   await fireEvent.click(screen.getByRole('button',{name:'Play cedar voice preview'}));
   expect((await screen.findByRole('alert')).textContent).toBe('GPT Live unavailable');
  } finally {preview.mockRestore();vi.unstubAllGlobals();}
 });
});
