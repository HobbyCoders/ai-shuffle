import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';
const api = vi.hoisted(() => ({ getVoiceCallState: vi.fn() }));
vi.mock('$lib/api/voice', async (original) => ({...await original<typeof import('$lib/api/voice')>(), ...api}));
vi.mock('$lib/services/voiceCall', () => ({
 voiceCallState: writable({sessionId:'call', status:'live', model:'gpt-live-1', startedAt:null,
  micMuted:false, agentMuted:false, agentSpeaking:false, userSpeaking:false, level:0}),
 hangup: vi.fn(), toggleMic: vi.fn(), toggleAgent: vi.fn(),
}));
import { voiceCallState } from '$lib/services/voiceCall';
import VoiceCallPanel from './VoiceCallPanel.svelte';
beforeEach(() => { api.getVoiceCallState.mockReset(); });
afterEach(cleanup);
it.each([
 ['gpt-live-1', 'GPT Live 1', 'Selected-model delegation', false],
 ['gpt-realtime-2.1', 'GPT Realtime 2.1', 'Realtime agent tools', true],
 ['gpt-realtime-2.1-mini', 'GPT Realtime 2.1 Mini', 'Realtime agent tools', true],
 ['gpt-realtime', 'GPT Realtime (legacy)', 'Realtime agent tools', true],
 ['fal-ai/personaplex/realtime', 'PersonaPlex', 'Full-duplex conversation', false],
])('shows the pinned %s engine and only its telemetry', async (model, label, mode, polls) => {
 voiceCallState.update(s => ({...s, model, sessionId:'call', status:'live'}));
 api.getVoiceCallState.mockResolvedValue({estimated_cost_usd:0.2,active_tasks:2});
 render(VoiceCallPanel, {sessionId:'call',compact:true});
 expect(screen.getByRole('heading', {name:label})).toBeTruthy();
 expect(screen.getByText(mode)).toBeTruthy();
 if (polls) {
  await screen.findByText(/2 agents working/);
  expect(api.getVoiceCallState).toHaveBeenCalledOnce();
 } else {
  expect(api.getVoiceCallState).not.toHaveBeenCalled();
  expect(screen.queryByText(/agents working/)).toBeNull();
 }
 if (model.startsWith('fal-ai/')) expect(screen.getByText('Audio connected')).toBeTruthy();
 expect(screen.getByRole('button', {name:'Mute mic'})).toBeTruthy();
 expect(screen.getByRole('button', {name:'Mute agent audio'})).toBeTruthy();
});
it('identifies Responses delegation without advertising app workers', () => {
 voiceCallState.update(s=>({...s,sessionId:'call',model:'gpt-live-1',options:{delegation_type:'responses'},status:'live'}));
 render(VoiceCallPanel,{sessionId:'call'});
 expect(screen.getByText('OpenAI Responses delegation')).toBeTruthy();
 expect(api.getVoiceCallState).not.toHaveBeenCalled();
});
it('does not restart telemetry when audio levels change', async () => {
 voiceCallState.update(s => ({...s, model:'gpt-realtime', sessionId:'call', status:'live'}));
 api.getVoiceCallState.mockResolvedValue({estimated_cost_usd:0.2,active_tasks:0});
 render(VoiceCallPanel, {sessionId:'call'});
 await waitFor(() => expect(api.getVoiceCallState).toHaveBeenCalledOnce());
 voiceCallState.update(s => ({...s, level:0.8, micMuted:true}));
 await screen.findByRole('button', {name:'Mic muted'});
 expect(api.getVoiceCallState).toHaveBeenCalledOnce();
});
it('does not apply a stale realtime poll to a streaming call', async () => {
 let resolve!: (value: any) => void;
 api.getVoiceCallState.mockReturnValue(new Promise(r => {resolve=r;}));
 voiceCallState.update(s => ({...s, model:'gpt-realtime', sessionId:'call', status:'live'}));
 render(VoiceCallPanel, {sessionId:'call'});
 await waitFor(() => expect(api.getVoiceCallState).toHaveBeenCalledOnce());
 voiceCallState.update(s => ({...s, model:'gpt-live-1'}));
 resolve({estimated_cost_usd:4,active_tasks:3});
 await screen.findByRole('heading', {name:'GPT Live 1'});
 expect(screen.queryByText(/agents working/)).toBeNull();
 expect(screen.queryByText(/~\$4/)).toBeNull();
});
