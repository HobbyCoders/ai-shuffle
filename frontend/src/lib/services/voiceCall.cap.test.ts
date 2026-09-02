/**
 * The call-length cap is terminal for the FE (VW5).
 *
 * The backend measures `voice_max_call_minutes` from the START of the call, not
 * from the current sideband attachment, so the FE's silent 50-minute
 * re-handshake must never be able to "reconnect past" it. It answers 409 on
 * `/secret` and `/call`, and the rule tested here is: a 409 ends the call with
 * the server's own message on screen — it is never retried.
 *
 * Only the API layer is mocked; the controller's own state machine runs for
 * real (no WebRTC is reached, because the cap is refused at the mint).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

const mintVoiceSecret = vi.fn();
const hangupVoiceCall = vi.fn();
const attachVoiceCall = vi.fn();

vi.mock('$lib/api/voice', () => ({
	mintVoiceSecret: (sessionId: string) => mintVoiceSecret(sessionId),
	hangupVoiceCall: (sessionId: string) => hangupVoiceCall(sessionId),
	attachVoiceCall: (sessionId: string, callId: string) => attachVoiceCall(sessionId, callId),
}));

import { startCallForSession, voiceCallState } from './voiceCall';

const CAP_DETAIL = 'Call length cap reached (240 minutes). Hang up and start a new call.';

beforeEach(() => {
	vi.clearAllMocks();
	hangupVoiceCall.mockResolvedValue({ ok: true });
	attachVoiceCall.mockResolvedValue({ ok: true });
	voiceCallState.set({
		sessionId: null,
		status: 'idle',
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
	});
});

describe('voiceCall — call-length cap', () => {
	it('(a) a 409 ends the call and shows the server message', async () => {
		mintVoiceSecret.mockRejectedValueOnce({ status: 409, detail: CAP_DETAIL });

		await startCallForSession('sess-1');

		const state = get(voiceCallState);
		expect(state.status).toBe('ended');
		expect(state.notice).toBe(CAP_DETAIL);
		// Not an error state: the call is over, nothing failed unexpectedly.
		expect(state.error).toBeNull();
	});

	it('(b) a 409 is not retried — one mint attempt, no attach', async () => {
		mintVoiceSecret.mockRejectedValueOnce({ status: 409, detail: CAP_DETAIL });

		await startCallForSession('sess-1');

		expect(mintVoiceSecret).toHaveBeenCalledTimes(1);
		expect(attachVoiceCall).not.toHaveBeenCalled();
	});

	it('(c) other failures still surface as an error, not a cap notice', async () => {
		mintVoiceSecret.mockRejectedValueOnce({ status: 502, detail: 'OpenAI is down' });

		await startCallForSession('sess-1');

		const state = get(voiceCallState);
		expect(state.status).toBe('error');
		expect(state.error).toBe('OpenAI is down');
		expect(state.notice).toBeNull();
	});
});
