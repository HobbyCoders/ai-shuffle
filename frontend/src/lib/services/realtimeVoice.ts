/**
 * Realtime voice client (WebRTC + Pipecat backend).
 *
 * Establishes a peer connection to `/api/v1/voice/realtime/offer`, streams
 * the local microphone, plays the remote bot audio, and consumes a sidechannel
 * WebSocket for transcripts + state.
 *
 * Usage:
 *   const session = new RealtimeVoiceSession({ projectId, sessionId });
 *   session.onTranscript = (text, final) => { ... };
 *   session.onState = (state) => { ... };
 *   await session.connect();
 *   // later:
 *   await session.disconnect();
 */

import { api } from '$lib/api/client';

export type VoiceState =
	| 'idle'
	| 'connecting'
	| 'connected'
	| 'listening'
	| 'thinking'
	| 'speaking'
	| 'reconnecting'
	| 'disconnected'
	| 'error';

export interface RealtimeVoiceOptions {
	projectId?: string;
	sessionId?: string;
	systemPrompt?: string;
	/** Optional HTMLAudioElement to attach the remote track to. */
	audioElement?: HTMLAudioElement;
	/** ICE servers, default none (local peer-to-peer). */
	iceServers?: RTCIceServer[];
}

export interface RealtimeVoiceCallbacks {
	onTranscript?: (text: string, isFinal: boolean, role: 'user' | 'assistant') => void;
	onState?: (state: VoiceState) => void;
	onError?: (error: Error) => void;
	onMetric?: (name: string, value: number) => void;
}

interface OfferResponse {
	voice_session_id: string;
	sdp: string;
	type: RTCSdpType;
	stt_provider: string;
	tts_provider: string;
}

interface EventMessage {
	type: 'transcript' | 'state' | 'closed' | 'metric';
	role?: 'user' | 'assistant';
	text?: string;
	timestamp?: number;
	closed?: boolean;
	last_activity?: number;
	reason?: string;
	name?: string;
	value?: number;
}

export class RealtimeVoiceSession {
	private pc: RTCPeerConnection | null = null;
	private localStream: MediaStream | null = null;
	private remoteStream: MediaStream | null = null;
	private eventsWs: WebSocket | null = null;
	private voiceSessionId: string | null = null;
	private state: VoiceState = 'idle';
	private closed = false;

	// Optional-attached audio sink (user-provided OR lazily created).
	private audioEl: HTMLAudioElement | null = null;
	private ownsAudioEl = false;

	public sttProvider = '';
	public ttsProvider = '';

	constructor(
		private opts: RealtimeVoiceOptions = {},
		private cb: RealtimeVoiceCallbacks = {}
	) {
		if (opts.audioElement) {
			this.audioEl = opts.audioElement;
		}
	}

	get currentState(): VoiceState {
		return this.state;
	}

	get id(): string | null {
		return this.voiceSessionId;
	}

	/** Open mic, negotiate SDP, connect events WS. */
	async connect(): Promise<void> {
		if (this.state !== 'idle' && this.state !== 'disconnected' && this.state !== 'error') {
			throw new Error(`Cannot connect from state: ${this.state}`);
		}

		this.setState('connecting');
		this.closed = false;

		try {
			this.localStream = await navigator.mediaDevices.getUserMedia({
				audio: {
					echoCancellation: true,
					noiseSuppression: true,
					autoGainControl: true,
					channelCount: 1,
					sampleRate: 16000
				},
				video: false
			});

			this.pc = new RTCPeerConnection({
				iceServers: this.opts.iceServers ?? []
			});

			// Add outbound track.
			this.localStream.getTracks().forEach((t) => this.pc!.addTrack(t, this.localStream!));

			// Request inbound audio transceiver (bot voice).
			this.pc.addTransceiver('audio', { direction: 'recvonly' });

			this.pc.ontrack = (ev) => this.attachRemoteStream(ev.streams[0]);
			this.pc.oniceconnectionstatechange = () => this.handleIceState();

			const offer = await this.pc.createOffer();
			await this.pc.setLocalDescription(offer);

			// Wait for ICE gathering to complete (simplifies SDP — no trickle).
			await this.waitForIceComplete(this.pc);

			const answer = await api.post<OfferResponse>('/voice/realtime/offer', {
				sdp: this.pc.localDescription!.sdp,
				type: this.pc.localDescription!.type,
				project_id: this.opts.projectId,
				session_id: this.opts.sessionId,
				system_prompt: this.opts.systemPrompt
			});

			this.voiceSessionId = answer.voice_session_id;
			this.sttProvider = answer.stt_provider;
			this.ttsProvider = answer.tts_provider;

			await this.pc.setRemoteDescription({
				type: answer.type,
				sdp: answer.sdp
			});

			this.openEventsWs();
			this.setState('connected');
		} catch (e) {
			this.setState('error');
			const err = e instanceof Error ? e : new Error(String(e));
			this.cb.onError?.(err);
			await this.cleanup();
			throw err;
		}
	}

	/** Hang up: close PC, revoke mic, close events WS, notify server. */
	async disconnect(): Promise<void> {
		if (this.closed) return;
		this.closed = true;

		if (this.voiceSessionId) {
			try {
				await api.post(`/voice/realtime/${this.voiceSessionId}/close`, {});
			} catch {
				/* best-effort */
			}
		}

		await this.cleanup();
		this.setState('disconnected');
	}

	/** Toggle microphone track enabled state (mute without tearing down PC). */
	setMuted(muted: boolean): void {
		this.localStream?.getAudioTracks().forEach((t) => (t.enabled = !muted));
	}

	// ------------------------------------------------------------------
	// Internals
	// ------------------------------------------------------------------

	private async cleanup(): Promise<void> {
		try {
			this.eventsWs?.close();
		} catch {
			/* ignore */
		}
		this.eventsWs = null;

		try {
			this.pc?.getSenders().forEach((s) => s.track?.stop());
			this.pc?.close();
		} catch {
			/* ignore */
		}
		this.pc = null;

		this.localStream?.getTracks().forEach((t) => t.stop());
		this.localStream = null;

		if (this.audioEl && this.ownsAudioEl) {
			this.audioEl.srcObject = null;
			this.audioEl.remove();
			this.audioEl = null;
			this.ownsAudioEl = false;
		} else if (this.audioEl) {
			this.audioEl.srcObject = null;
		}
		this.remoteStream = null;
	}

	private attachRemoteStream(stream: MediaStream): void {
		this.remoteStream = stream;
		if (!this.audioEl) {
			this.audioEl = document.createElement('audio');
			this.audioEl.autoplay = true;
			this.audioEl.style.display = 'none';
			document.body.appendChild(this.audioEl);
			this.ownsAudioEl = true;
		}
		this.audioEl.srcObject = stream;
		this.audioEl.play().catch((e) => {
			// iOS may block autoplay until user gesture — surface as error so UI can prompt.
			this.cb.onError?.(new Error(`Autoplay blocked: ${e.message}`));
		});
	}

	private handleIceState(): void {
		if (!this.pc) return;
		const s = this.pc.iceConnectionState;
		if (s === 'connected' || s === 'completed') {
			this.setState('connected');
		} else if (s === 'disconnected') {
			this.setState('reconnecting');
		} else if (s === 'failed' || s === 'closed') {
			if (!this.closed) this.setState('disconnected');
		}
	}

	private openEventsWs(): void {
		if (!this.voiceSessionId) return;
		const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
		const url = `${proto}//${window.location.host}/api/v1/voice/realtime/${this.voiceSessionId}/events`;
		const ws = new WebSocket(url);

		ws.onmessage = (ev) => {
			try {
				const msg: EventMessage = JSON.parse(ev.data);
				if (msg.type === 'transcript' && msg.text) {
					this.cb.onTranscript?.(msg.text, true, msg.role ?? 'user');
				} else if (msg.type === 'closed') {
					this.setState('disconnected');
				} else if (msg.type === 'metric' && msg.name && typeof msg.value === 'number') {
					this.cb.onMetric?.(msg.name, msg.value);
				}
			} catch {
				/* ignore malformed */
			}
		};

		ws.onclose = () => {
			if (!this.closed) {
				this.setState('reconnecting');
			}
		};

		ws.onerror = () => {
			/* errors surface via onclose */
		};

		this.eventsWs = ws;
	}

	private waitForIceComplete(pc: RTCPeerConnection, timeoutMs = 2000): Promise<void> {
		return new Promise((resolve) => {
			if (pc.iceGatheringState === 'complete') {
				resolve();
				return;
			}
			const timer = setTimeout(() => resolve(), timeoutMs);
			const check = () => {
				if (pc.iceGatheringState === 'complete') {
					clearTimeout(timer);
					pc.removeEventListener('icegatheringstatechange', check);
					resolve();
				}
			};
			pc.addEventListener('icegatheringstatechange', check);
		});
	}

	private setState(s: VoiceState): void {
		if (this.state === s) return;
		this.state = s;
		this.cb.onState?.(s);
	}
}

// --------------------------------------------------------------------------
// Capabilities probe (UI can call this before attempting to connect)
// --------------------------------------------------------------------------

export interface VoiceCapabilities {
	stt_providers: Array<{ id: string; name: string; available: boolean; streaming: boolean }>;
	tts_providers: Array<{ id: string; name: string; available: boolean; streaming: boolean }>;
	pipecat_available: boolean;
	error?: string | null;
}

export async function fetchVoiceCapabilities(): Promise<VoiceCapabilities> {
	return api.get<VoiceCapabilities>('/voice/realtime/capabilities');
}
