import { describe, expect, it } from 'vitest';
import { usesAudioStream, usesDelegation } from './voice-engines';

describe('unofficial Codex voice capability', () => {
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
