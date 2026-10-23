import { describe, expect, it } from 'vitest';
import { SUPERGROK_VOICE_MODEL, usesAudioStream, usesDelegation } from './voice-engines';

describe('ChatGPT subscription (Codex) voice capability', () => {
 it('uses the server PCM leg and same-chat delegation', () => {
  expect(usesAudioStream('gpt-live-1-codex')).toBe(true);
  expect(usesDelegation('gpt-live-1-codex')).toBe(true);
 });
 it('does not alter supported or unknown engines', () => {
  expect(usesAudioStream('gpt-live-1')).toBe(true);
  expect(usesAudioStream('grok-voice-latest')).toBe(true);
  expect(usesAudioStream('unknown')).toBe(false);
  expect(usesDelegation('gpt-realtime')).toBe(false);
 });
});

describe('SuperGrok subscription Grok voice capability', () => {
 it('uses the server PCM leg and same-chat delegation like API-key Grok Voice', () => {
  expect(SUPERGROK_VOICE_MODEL).toBe('grok-voice-supergrok');
  expect(usesAudioStream(SUPERGROK_VOICE_MODEL)).toBe(true);
  expect(usesDelegation(SUPERGROK_VOICE_MODEL)).toBe(true);
 });
});
