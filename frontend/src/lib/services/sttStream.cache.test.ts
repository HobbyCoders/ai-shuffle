import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const transport = vi.hoisted(() => ({put:vi.fn()}));
vi.mock('$lib/api/client', () => ({api:transport}));
vi.mock('$app/environment', () => ({browser:true}));
import {fetchSttCapability, invalidateSttCapability} from './sttStream';
import {resourceSync} from './resource-sync';
import {bindSessionOwner} from '$lib/stores/session-owner';
import {saveVoiceSettings} from '$lib/api/voice';

const response = (model: string) => ({ok:true,json:async()=>({model,supported:model.startsWith('openai/')})});
beforeEach(()=>{vi.useFakeTimers();invalidateSttCapability();transport.put.mockReset();});
afterEach(()=>{resourceSync.clear();vi.useRealTimers();vi.unstubAllGlobals();});
describe('voice input model capability cache', () => {
 it('reprobes immediately after saving the dedicated voice model', async () => {
  const fetch=vi.fn().mockResolvedValueOnce(response('fal-ai/whisper')).mockResolvedValue(response('openai/gpt-4o-transcribe'));
  vi.stubGlobal('fetch',fetch);
  await fetchSttCapability(); await fetchSttCapability();
  expect(fetch).toHaveBeenCalledTimes(1);
  transport.put.mockResolvedValue({voice_input_model:'openai/gpt-4o-transcribe'});
  await saveVoiceSettings({voice_input_model:'openai/gpt-4o-transcribe'});
  expect((await fetchSttCapability()).model).toBe('openai/gpt-4o-transcribe');
  expect(fetch).toHaveBeenCalledTimes(2);
 });
 it('drops the probe on remote settings changes', async () => {
  const fetch=vi.fn().mockResolvedValue(response('fal-ai/whisper')); vi.stubGlobal('fetch',fetch);
  await fetchSttCapability(); resourceSync.invalidate(['settings']); await vi.advanceTimersByTimeAsync(100); await fetchSttCapability();
  expect(fetch).toHaveBeenCalledTimes(2);
 });
 it('does not cache an old response that arrives after invalidation', async () => {
  let finish!: (value:unknown)=>void;
  const fetch=vi.fn().mockReturnValueOnce(new Promise(resolve=>finish=resolve)).mockResolvedValue(response('openai/gpt-4o-transcribe'));
  vi.stubGlobal('fetch',fetch);
  const old=fetchSttCapability(); invalidateSttCapability(); finish(response('fal-ai/whisper')); await old;
  expect((await fetchSttCapability()).model).toBe('openai/gpt-4o-transcribe');
 });
 it('does not reuse a different account’s probe', async () => {
  const fetch=vi.fn().mockResolvedValue(response('fal-ai/whisper')); vi.stubGlobal('fetch',fetch);
  await fetchSttCapability(); bindSessionOwner('voice-account-a','voice-account-b'); await fetchSttCapability();
  expect(fetch).toHaveBeenCalledTimes(2);
 });
});
