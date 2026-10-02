/**
 * Ruling VM18: a live call OWNS the chat composer's slot — and nothing else.
 *
 * `isCallSurfaceActive` is the single gate ChatView uses to decide whether the
 * text input or the call controls render in the composer slot, so the two
 * properties that matter are tested here directly:
 *
 *   1. Only an in-flight call (connecting / live / reconnecting) takes the slot;
 *      every terminal status hands the composer straight back.
 *   2. The call must belong to the session being VIEWED — the controller is a
 *      singleton, so a call on session A must not swallow session B's composer.
 */
import { describe, it, expect, vi } from 'vitest';

vi.mock('$lib/api/voice', () => ({
	mintVoiceSecret: vi.fn(),
	hangupVoiceCall: vi.fn(),
	attachVoiceCall: vi.fn(),
}));

import { isCallSurfaceActive, type VoiceCallStoreState, type VoiceCallStatus } from './voiceCall';

function stateFor(sessionId: string | null, status: VoiceCallStatus): VoiceCallStoreState {
	return {
		sessionId,
		status,
		micMuted: false,
		agentMuted: false,
		level: 0,
		captions: [],
		error: null,
		startedAt: null,
		notice: null,
		model: null,
		agentSpeaking: false,
		userSpeaking: false,
		endReason: null,
	};
}

describe('isCallSurfaceActive — composer-slot gate (VM18)', () => {
	it('takes the slot for every in-flight status', () => {
		for (const status of ['connecting', 'live', 'reconnecting'] as VoiceCallStatus[]) {
			expect(isCallSurfaceActive(stateFor('sess-1', status), 'sess-1')).toBe(true);
		}
	});

	it('gives the composer back on every terminal status', () => {
		for (const status of ['idle', 'ended', 'error'] as VoiceCallStatus[]) {
			expect(isCallSurfaceActive(stateFor('sess-1', status), 'sess-1')).toBe(false);
		}
	});

	it('is session-scoped — another session keeps its normal composer', () => {
		const live = stateFor('sess-1', 'live');
		expect(isCallSurfaceActive(live, 'sess-2')).toBe(false);
		expect(isCallSurfaceActive(live, null)).toBe(false);
		expect(isCallSurfaceActive(live, undefined)).toBe(false);
	});

	it('never takes the slot when the controller is unbound', () => {
		expect(isCallSurfaceActive(stateFor(null, 'idle'), 'sess-1')).toBe(false);
	});
});
