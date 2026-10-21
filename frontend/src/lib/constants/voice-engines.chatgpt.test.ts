import { describe, expect, it } from 'vitest';
import { voiceModelLabel } from '$lib/api/voice';
import { CODEX_VOICE_MODEL, usesAudioStream, usesDelegation } from './voice-engines';

describe('unofficial ChatGPT voice engine id', () => {
	it('is the opt-in engine, labelled unofficial and unsupported, and uses the server audio path', () => {
		expect(CODEX_VOICE_MODEL).toBe('gpt-live-1-codex');
		expect(voiceModelLabel(CODEX_VOICE_MODEL)).toBe('ChatGPT voice (unofficial, unsupported)');
		expect(usesAudioStream(CODEX_VOICE_MODEL)).toBe(true);
		expect(usesDelegation(CODEX_VOICE_MODEL)).toBe(true);
		expect(voiceModelLabel('gpt-live-1')).toBe('GPT Live 1');
	});
});
