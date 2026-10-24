import { describe, expect, it } from 'vitest';
import { voiceModelLabel } from '$lib/api/voice';
import { CODEX_VOICE_MODEL, LIVE_11_VOICE_MODEL, isLiveApiModel, usesAudioStream, usesDelegation } from './voice-engines';

describe('ChatGPT subscription voice engine id', () => {
	it('is the opt-in engine, labelled by its subscription, and uses the server audio path', () => {
		expect(CODEX_VOICE_MODEL).toBe('gpt-live-1-codex');
		expect(voiceModelLabel(CODEX_VOICE_MODEL)).toBe('GPT-Live-1.1 · ChatGPT subscription');
		expect(usesAudioStream(CODEX_VOICE_MODEL)).toBe(true);
		expect(usesDelegation(CODEX_VOICE_MODEL)).toBe(true);
		expect(voiceModelLabel('gpt-live-1')).toBe('GPT Live 1');
	});

	it('treats API GPT-Live-1.1 like GPT Live 1, never like the subscription engine', () => {
		expect(voiceModelLabel(LIVE_11_VOICE_MODEL)).toBe('GPT-Live-1.1');
		expect(isLiveApiModel(LIVE_11_VOICE_MODEL)).toBe(true);
		expect(isLiveApiModel('gpt-live-1')).toBe(true);
		expect(isLiveApiModel(CODEX_VOICE_MODEL)).toBe(false);
		expect(usesAudioStream(LIVE_11_VOICE_MODEL)).toBe(true);
		expect(usesDelegation(LIVE_11_VOICE_MODEL)).toBe(true);
		expect(usesDelegation(LIVE_11_VOICE_MODEL, { delegation_type: 'responses' })).toBe(false);
	});
});
