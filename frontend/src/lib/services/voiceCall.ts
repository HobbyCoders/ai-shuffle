/**
 * WebRTC voice-call controller (wave VW2 of docs/VOICE_MODE_PLAN.md).
 *
 * One singleton per active call. Transport is ruling VM2: the browser talks
 * DIRECTLY to OpenAI over WebRTC (audio never transits our backend) and the
 * backend joins the same call out-of-band with a "sideband" WebSocket keyed on
 * the `call_id` we hand it. So this module owns exactly:
 *
 *   mic capture → RTCPeerConnection → SDP offer POSTed to OpenAI →
 *   answer applied → `call_id` read off the `Location` header → POSTed to our
 *   backend → remote audio played → data-channel events parsed for captions.
 *
 * Two deliberate non-responsibilities:
 *
 * * **No transcript persistence.** Captions here are ephemeral UI. The durable
 *   transcript is written backend-side from the sideband (VW3 / ruling VM9).
 * * **No tool handling.** Tools are declared and serviced on the sideband so
 *   they are invisible to (and untamperable by) the client.
 *
 * The 60-minute OpenAI session wall is bridged at the 50-minute mark by a
 * silent re-handshake (ruling VM15, FE half): mint a fresh secret, build a
 * SECOND peer connection, swap playback + mic onto it once it is connected,
 * register the new `call_id`, then close the old one. The mic MediaStream is
 * owned by the controller, not by a connection, so the swap needs no second
 * `getUserMedia` prompt.
 *
 * That re-handshake is exactly why the backend's call-length cap answers **409**
 * (VW5): the cap is measured from the call's start, not from the current
 * attachment, so a 409 from `/secret` or `/call` means "this call is over". It
 * is terminal by design — the call ends with the server's message shown and the
 * reconnect loop stops. A 409 is never retried.
 *
 * Everything that talks to a VW3-owned endpoint degrades gracefully — see
 * `$lib/api/voice`. With the bridge unmerged the call still connects and
 * speaks; it just has no backend brain and cannot re-handshake.
 */

import { get, writable } from 'svelte/store';
import { attachVoiceCall, hangupVoiceCall, mintVoiceSecret } from '$lib/api/voice';

// ============================================================================
// Public state
// ============================================================================

export type VoiceCallStatus =
	| 'idle'
	| 'connecting'
	| 'live'
	| 'reconnecting'
	| 'ended'
	| 'error';

export interface VoiceCaption {
	/** OpenAI item id when known, else a synthetic key. */
	id: string;
	role: 'user' | 'assistant';
	text: string;
	/** False while deltas are still streaming in. */
	final: boolean;
}

export interface VoiceCallStoreState {
	/** The ai-shuffle session this call belongs to (null when idle). */
	sessionId: string | null;
	status: VoiceCallStatus;
	micMuted: boolean;
	agentMuted: boolean;
	/** Smoothed mic RMS, 0–1. */
	level: number;
	captions: VoiceCaption[];
	error: string | null;
	/** Epoch ms the call went live. */
	startedAt: number | null;
	/** Non-fatal advisory rendered in the caption area (e.g. reconnect skipped). */
	notice: string | null;
	model: string | null;
}

const INITIAL: VoiceCallStoreState = {
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
};

export const voiceCallState = writable<VoiceCallStoreState>({ ...INITIAL });

function patch(next: Partial<VoiceCallStoreState>): void {
	voiceCallState.update((s) => ({ ...s, ...next }));
}

// ============================================================================
// Tunables
// ============================================================================

const OPENAI_CALLS_URL = 'https://api.openai.com/v1/realtime/calls';

/**
 * OpenAI hard-caps a realtime session at 60 minutes. Re-handshake 10 minutes
 * early so a slow mint still lands well inside the wall (ruling VM15).
 */
const RECONNECT_AFTER_MS = 50 * 60 * 1000;

/** How long to wait for the new peer connection during a re-handshake. */
const CONNECT_TIMEOUT_MS = 20_000;

/** Captions are ephemeral UI — keep the tail only. */
const MAX_CAPTIONS = 60;

const MIC_CONSTRAINTS: MediaTrackConstraints = {
	echoCancellation: true,
	noiseSuppression: true,
	autoGainControl: true,
};

// ============================================================================
// Private state
// ============================================================================

/** One peer connection + its data channel. A re-handshake creates a second. */
interface Connection {
	pc: RTCPeerConnection;
	dc: RTCDataChannel;
	callId: string | null;
}

let sessionId: string | null = null;
let connection: Connection | null = null;

/** Owned by the controller (survives re-handshakes), stopped only on hangup. */
let micStream: MediaStream | null = null;
let audioEl: HTMLAudioElement | null = null;

let audioContext: AudioContext | null = null;
let analyser: AnalyserNode | null = null;
let levelFrame: number | null = null;

let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

/** Bumped on every start/hangup so a stale async continuation can bail out. */
let generation = 0;

// ============================================================================
// Microphone
// ============================================================================

/** Human-readable reason a `getUserMedia` attempt failed. */
function micErrorMessage(error: unknown): string {
	const err = error as { name?: string; message?: string };
	if (err?.name === 'NotAllowedError') {
		return 'Microphone access denied. Please allow microphone access in your browser settings.';
	}
	if (err?.name === 'NotFoundError') {
		return 'No microphone found. Please connect a microphone and try again.';
	}
	return `Could not start the microphone: ${err?.message || 'Unknown error'}`;
}

async function acquireMic(): Promise<MediaStream> {
	// Same secure-context precheck (and the same chrome://flags hint) the STT
	// recorder documents — mic capture needs HTTPS or localhost.
	if (typeof navigator === 'undefined' || !navigator.mediaDevices) {
		throw new Error(
			'Microphone requires a secure context (HTTPS or localhost). In Chrome, you can allow this origin at chrome://flags/#unsafely-treat-insecure-origin-as-secure'
		);
	}
	return navigator.mediaDevices.getUserMedia({ audio: MIC_CONSTRAINTS });
}

/** AudioContext + analyser RMS loop feeding `level` (orb animation). */
function startLevelMonitor(stream: MediaStream): void {
	stopLevelMonitor();
	try {
		audioContext = new AudioContext();
		analyser = audioContext.createAnalyser();
		analyser.fftSize = 256;
		analyser.smoothingTimeConstant = 0.8;
		audioContext.createMediaStreamSource(stream).connect(analyser);
	} catch (e) {
		console.warn('[voice] mic level monitoring unavailable:', e);
		analyser = null;
		return;
	}

	const data = new Uint8Array(analyser.frequencyBinCount);
	const tick = () => {
		if (!analyser) return;
		analyser.getByteFrequencyData(data);
		let sum = 0;
		for (let i = 0; i < data.length; i++) {
			const n = data[i] / 255;
			sum += n * n;
		}
		const rms = Math.sqrt(sum / data.length);
		voiceCallState.update((s) => {
			const smoothed = s.level * 0.7 + rms * 0.3;
			// Skip imperceptible updates so the orb doesn't re-render every frame.
			return Math.abs(smoothed - s.level) < 0.004 ? s : { ...s, level: smoothed };
		});
		levelFrame = requestAnimationFrame(tick);
	};
	levelFrame = requestAnimationFrame(tick);
}

function stopLevelMonitor(): void {
	if (levelFrame !== null) {
		cancelAnimationFrame(levelFrame);
		levelFrame = null;
	}
	analyser = null;
	if (audioContext) {
		audioContext.close().catch(() => {});
		audioContext = null;
	}
	patch({ level: 0 });
}

// ============================================================================
// Playback
// ============================================================================

/**
 * The single `<audio>` sink for the agent's voice. Reused across re-handshakes
 * so the mute state (and the browser's autoplay grant) survives the swap.
 */
function ensureAudioElement(): HTMLAudioElement {
	if (audioEl) return audioEl;
	const el = document.createElement('audio');
	el.autoplay = true;
	// Keeps iOS Safari from taking the element fullscreen.
	el.setAttribute('playsinline', '');
	el.style.display = 'none';
	document.body.appendChild(el);
	audioEl = el;
	return el;
}

function teardownAudioElement(): void {
	if (!audioEl) return;
	audioEl.srcObject = null;
	audioEl.remove();
	audioEl = null;
}

// ============================================================================
// Captions (ephemeral — the durable transcript is backend-owned, VM9)
// ============================================================================

function appendCaption(
	id: string,
	role: 'user' | 'assistant',
	chunk: string,
	final: boolean
): void {
	if (!chunk && !final) return;
	voiceCallState.update((s) => {
		const captions = [...s.captions];
		const idx = captions.findIndex((c) => c.id === id && c.role === role);
		if (idx >= 0) {
			const prev = captions[idx];
			captions[idx] = {
				...prev,
				// A `.done`/`.completed` event carries the WHOLE transcript, so it
				// replaces the accumulated deltas rather than appending to them.
				text: final ? chunk || prev.text : prev.text + chunk,
				final,
			};
		} else {
			captions.push({ id, role, text: chunk, final });
		}
		return { ...s, captions: captions.slice(-MAX_CAPTIONS) };
	});
}

/**
 * Parse one realtime data-channel event into caption state.
 *
 * User side comes from input-audio transcription, assistant side from the
 * output-audio transcript. Everything else (tool calls, session lifecycle) is
 * the sideband's business and is ignored here.
 */
function handleRealtimeEvent(raw: string): void {
	let event: any;
	try {
		event = JSON.parse(raw);
	} catch {
		return;
	}
	const type: string = event?.type ?? '';
	const itemId: string = event?.item_id || event?.response_id || 'pending';

	switch (type) {
		case 'conversation.item.input_audio_transcription.delta':
			appendCaption(`u:${itemId}`, 'user', event.delta ?? '', false);
			break;
		case 'conversation.item.input_audio_transcription.completed':
			appendCaption(`u:${itemId}`, 'user', event.transcript ?? '', true);
			break;
		case 'response.output_audio_transcript.delta':
			appendCaption(`a:${itemId}`, 'assistant', event.delta ?? '', false);
			break;
		case 'response.output_audio_transcript.done':
			appendCaption(`a:${itemId}`, 'assistant', event.transcript ?? '', true);
			break;
		case 'error':
			console.warn('[voice] realtime error event:', event.error ?? event);
			break;
		default:
			break;
	}
}

// ============================================================================
// Handshake
// ============================================================================

/** Read the call id off the `Location` header (`/v1/realtime/calls/<id>`). */
function callIdFromLocation(location: string | null): string | null {
	if (!location) return null;
	const trimmed = location.split('?')[0].replace(/\/+$/, '');
	const last = trimmed.split('/').pop();
	return last || null;
}

/** Resolve once the peer connection reaches `connected`, or reject on failure. */
function waitForConnected(pc: RTCPeerConnection, timeoutMs: number): Promise<void> {
	if (pc.connectionState === 'connected') return Promise.resolve();
	return new Promise((resolve, reject) => {
		const done = (fn: () => void) => {
			clearTimeout(timer);
			pc.removeEventListener('connectionstatechange', onChange);
			fn();
		};
		const onChange = () => {
			if (pc.connectionState === 'connected') done(resolve);
			else if (pc.connectionState === 'failed' || pc.connectionState === 'closed') {
				done(() => reject(new Error('The voice connection failed to establish.')));
			}
		};
		const timer = setTimeout(
			() => done(() => reject(new Error('Timed out connecting to the voice service.'))),
			timeoutMs
		);
		pc.addEventListener('connectionstatechange', onChange);
	});
}

/**
 * Build a peer connection, complete the SDP exchange with OpenAI and register
 * the resulting `call_id` with our backend. Does NOT touch global call state —
 * the caller decides whether this becomes the live connection.
 */
async function openConnection(
	clientSecret: string,
	model: string,
	stream: MediaStream
): Promise<Connection> {
	const pc = new RTCPeerConnection();

	// The events channel must exist BEFORE the offer so it is negotiated in the
	// same exchange (there is no second round trip to add it later).
	const dc = pc.createDataChannel('oai-events');
	dc.addEventListener('message', (e) => handleRealtimeEvent(String(e.data)));

	pc.addEventListener('track', (e) => {
		const el = ensureAudioElement();
		el.srcObject = e.streams[0] ?? new MediaStream([e.track]);
		el.muted = get(voiceCallState).agentMuted;
		el.play().catch((err) => console.warn('[voice] playback blocked:', err));
	});

	const [track] = stream.getAudioTracks();
	if (!track) throw new Error('No microphone track available.');
	pc.addTrack(track, stream);

	const offer = await pc.createOffer();
	await pc.setLocalDescription(offer);

	let response: Response;
	try {
		response = await fetch(`${OPENAI_CALLS_URL}?model=${encodeURIComponent(model)}`, {
			method: 'POST',
			headers: {
				Authorization: `Bearer ${clientSecret}`,
				'Content-Type': 'application/sdp',
			},
			body: offer.sdp ?? '',
		});
	} catch (e) {
		pc.close();
		throw new Error(
			`Could not reach OpenAI to start the call: ${e instanceof Error ? e.message : String(e)}`
		);
	}

	if (!response.ok) {
		const body = (await response.text().catch(() => '')).slice(0, 300);
		pc.close();
		throw new Error(`OpenAI rejected the call (HTTP ${response.status}): ${body}`);
	}

	const answer = await response.text();
	await pc.setRemoteDescription({ type: 'answer', sdp: answer });

	// The call id lives in the Location header; without it the backend cannot
	// join its sideband, but the audio leg still works — warn, don't fail.
	const callId = callIdFromLocation(response.headers.get('Location'));
	if (!callId) {
		console.warn(
			'[voice] no call id in the OpenAI Location header — the backend cannot attach its sideband.'
		);
	}

	return { pc, dc, callId };
}

function closeConnection(conn: Connection | null): void {
	if (!conn) return;
	try {
		conn.dc.close();
	} catch {
		/* already closed */
	}
	try {
		conn.pc.close();
	} catch {
		/* already closed */
	}
}

// ============================================================================
// Call-length cap (VW5) — a 409 ends the call, it is never retried
// ============================================================================

/**
 * The server's message when a request was refused by the call-length cap, or
 * `null` for any other failure.
 */
function callCapMessage(e: unknown): string | null {
	const err = e as { status?: number; detail?: string } | null;
	if (!err || typeof err !== 'object' || err.status !== 409) return null;
	return err.detail || 'This call reached its length cap. Hang up and start a new call.';
}

/**
 * End the call because the cap fired, leaving the reason on screen.
 *
 * The hangup notification matters: it is what clears the server's grace window,
 * so the "start a new call" the message asks for actually works.
 */
async function endForCap(sessionIdIn: string | null, message: string): Promise<void> {
	await teardown({ notifyBackend: true, status: 'ended' });
	patch({ sessionId: sessionIdIn, notice: message });
}

// ============================================================================
// Reconnect at the 60-minute wall (ruling VM15, FE half)
// ============================================================================

function armReconnectTimer(): void {
	clearReconnectTimer();
	reconnectTimer = setTimeout(() => {
		void reconnect();
	}, RECONNECT_AFTER_MS);
}

function clearReconnectTimer(): void {
	if (reconnectTimer !== null) {
		clearTimeout(reconnectTimer);
		reconnectTimer = null;
	}
}

/**
 * Silently re-handshake onto a fresh realtime session.
 *
 * Order matters: the NEW connection must be established (and its call id
 * registered) before the old one is torn down, so there is no audio gap beyond
 * the swap itself.
 */
async function reconnect(): Promise<void> {
	const myGeneration = generation;
	const current = get(voiceCallState);
	if (current.status !== 'live' || !sessionId || !micStream) return;

	patch({ status: 'reconnecting' });
	const previous = connection;

	try {
		const cred = await mintVoiceSecret(sessionId);
		if (myGeneration !== generation) return;
		if (!cred) {
			// VW3 not merged: no re-mint route. Surface it near the captions and
			// let the call run out its wall rather than dropping it silently.
			patch({
				status: 'live',
				notice:
					'Approaching the 60-minute limit for this call — automatic reconnect is not available yet, so the call may end shortly.',
			});
			return;
		}

		const next = await openConnection(cred.client_secret, cred.model, micStream);
		if (myGeneration !== generation) {
			closeConnection(next);
			return;
		}
		await waitForConnected(next.pc, CONNECT_TIMEOUT_MS);
		if (myGeneration !== generation) {
			closeConnection(next);
			return;
		}

		connection = next;
		applyMicMuted(get(voiceCallState).micMuted);
		if (next.callId) await attachVoiceCall(sessionId, next.callId);

		closeConnection(previous);
		patch({ status: 'live', model: cred.model, notice: null });
		armReconnectTimer();
	} catch (e) {
		if (myGeneration !== generation) return;
		// The call-length cap is terminal: reconnecting is precisely what must
		// NOT happen (it would restart the clock), so end the call instead.
		const capped = callCapMessage(e);
		if (capped) {
			await endForCap(sessionId, capped);
			return;
		}
		console.warn('[voice] reconnect failed:', e);
		// The old connection is still up until the wall actually hits — stay live
		// and tell the user what to expect.
		patch({
			status: 'live',
			notice: `Could not refresh the call before the 60-minute limit (${
				e instanceof Error ? e.message : String(e)
			}). It may end shortly.`,
		});
	}
}

// ============================================================================
// Controls
// ============================================================================

function applyMicMuted(muted: boolean): void {
	micStream?.getAudioTracks().forEach((t) => {
		t.enabled = !muted;
	});
}

/** Mute/unmute the microphone (the track stays attached; it is just disabled). */
export function setMicMuted(muted: boolean): void {
	applyMicMuted(muted);
	patch({ micMuted: muted });
}

export function toggleMic(): void {
	setMicMuted(!get(voiceCallState).micMuted);
}

/** Mute/unmute the agent's voice (playback only — the model keeps talking). */
export function setAgentMuted(muted: boolean): void {
	if (audioEl) audioEl.muted = muted;
	patch({ agentMuted: muted });
}

export function toggleAgent(): void {
	setAgentMuted(!get(voiceCallState).agentMuted);
}

// ============================================================================
// Lifecycle
// ============================================================================

/**
 * Start a call on `sessionIdIn` using an already-minted credential (the one
 * `POST /voice/sessions` returned).
 */
export async function startCall(
	sessionIdIn: string,
	cred: { client_secret: string; model: string }
): Promise<void> {
	await teardown({ notifyBackend: false, status: 'idle' });
	const myGeneration = ++generation;

	sessionId = sessionIdIn;
	voiceCallState.set({
		...INITIAL,
		sessionId: sessionIdIn,
		status: 'connecting',
		model: cred.model,
	});

	try {
		micStream = await acquireMic();
		if (myGeneration !== generation) return;

		const conn = await openConnection(cred.client_secret, cred.model, micStream);
		if (myGeneration !== generation) {
			closeConnection(conn);
			return;
		}
		connection = conn;

		await waitForConnected(conn.pc, CONNECT_TIMEOUT_MS);
		if (myGeneration !== generation) return;

		startLevelMonitor(micStream);
		if (conn.callId) await attachVoiceCall(sessionIdIn, conn.callId);

		conn.pc.addEventListener('connectionstatechange', () => {
			if (myGeneration !== generation) return;
			const s = conn.pc.connectionState;
			if (s === 'failed') {
				patch({ status: 'error', error: 'The voice connection dropped.' });
			}
		});

		patch({ status: 'live', startedAt: Date.now() });
		armReconnectTimer();
	} catch (e) {
		if (myGeneration !== generation) return;
		const capped = callCapMessage(e);
		if (capped) {
			await endForCap(sessionIdIn, capped);
			return;
		}
		const message =
			e instanceof Error && e.name ? micErrorOrMessage(e) : String(e ?? 'Unknown error');
		console.error('[voice] failed to start call:', e);
		await teardown({ notifyBackend: false, status: 'error' });
		patch({ sessionId: sessionIdIn, status: 'error', error: message });
	}
}

/** getUserMedia rejections get the friendly mapping; everything else is verbatim. */
function micErrorOrMessage(e: Error): string {
	if (e.name === 'NotAllowedError' || e.name === 'NotFoundError') return micErrorMessage(e);
	return e.message;
}

/**
 * Start a call on an existing voice session by re-minting its credential.
 *
 * This is the "Start call" button on a voice session that has no live call.
 * The re-mint route is VW3's, so a missing route is reported as a plain,
 * non-fatal message rather than an error state the user cannot clear.
 */
export async function startCallForSession(sessionIdIn: string): Promise<void> {
	patch({ sessionId: sessionIdIn, status: 'connecting', error: null, notice: null });
	let cred: Awaited<ReturnType<typeof mintVoiceSecret>>;
	try {
		cred = await mintVoiceSecret(sessionIdIn);
	} catch (e) {
		const capped = callCapMessage(e);
		if (capped) {
			// Nothing is up yet, so there is nothing to tear down — just say why.
			patch({ status: 'ended', notice: capped, error: null });
			return;
		}
		const detail = (e as { detail?: string })?.detail;
		patch({
			status: 'error',
			error: detail || (e instanceof Error ? e.message : 'Could not start the call.'),
		});
		return;
	}
	if (!cred) {
		patch({
			status: 'idle',
			error: 'Call backend not available yet — the voice bridge (VW3) is not deployed.',
		});
		return;
	}
	await startCall(sessionIdIn, cred);
}

/**
 * End the call. The session row stays — only the realtime call is torn down
 * (ruling VM12: dispatched agents survive a hangup).
 */
export async function hangup(): Promise<void> {
	await teardown({ notifyBackend: true, status: 'ended' });
}

async function teardown(opts: {
	notifyBackend: boolean;
	status: VoiceCallStatus;
}): Promise<void> {
	generation++;
	clearReconnectTimer();
	stopLevelMonitor();

	const endedSession = sessionId;
	closeConnection(connection);
	connection = null;

	micStream?.getTracks().forEach((t) => t.stop());
	micStream = null;
	teardownAudioElement();
	sessionId = null;

	voiceCallState.set({ ...INITIAL, sessionId: endedSession, status: opts.status });

	if (opts.notifyBackend && endedSession) {
		try {
			await hangupVoiceCall(endedSession);
		} catch (e) {
			console.warn('[voice] hangup notification failed:', e);
		}
	}
}

/**
 * The statuses that own the composer slot (ruling VM18).
 *
 * `connecting`/`live`/`reconnecting` are "a call is up on this session" — the
 * chat composer is replaced by the call controls for exactly these. Every
 * terminal status (`idle`/`ended`/`error`) gives the composer straight back.
 */
const ACTIVE_CALL_STATUSES: readonly VoiceCallStatus[] = ['connecting', 'live', 'reconnecting'];

/**
 * Pure predicate: does `state` describe an in-flight call belonging to
 * `sessionId`?
 *
 * The controller is a singleton, so the session check is what stops a call on
 * one session from swallowing another session's composer when the user switches
 * chats mid-call.
 */
export function isCallSurfaceActive(
	state: VoiceCallStoreState,
	sessionId: string | null | undefined
): boolean {
	if (!sessionId) return false;
	return state.sessionId === sessionId && ACTIVE_CALL_STATUSES.includes(state.status);
}

/** Is a call live (or coming up) for this session? Snapshot read of the store. */
export function isCallActiveFor(id: string | null | undefined): boolean {
	return isCallSurfaceActive(get(voiceCallState), id);
}
