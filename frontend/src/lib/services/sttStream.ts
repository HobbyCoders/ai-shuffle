/**
 * Realtime STT client.
 *
 * Captures the microphone, resamples to the 24 kHz mono PCM16 the realtime API
 * wants, and streams it to `/api/v1/settings/transcribe/stream` (a server-side
 * proxy onto OpenAI's realtime transcription session). Text comes back while
 * the user is still talking: `onDelta` grows the in-flight segment, `onFinal`
 * seals it when server VAD or the client's stop commits the turn.
 *
 * Capture path: an AudioContext pinned to 24 kHz does the resampling in native
 * code, so the JS resampler below is a fallback for browsers that refuse the
 * requested rate. An AudioWorklet batches raw frames off the audio thread;
 * ScriptProcessorNode is the fallback where worklets are unavailable.
 *
 * The service owns no UI state — it reports through handlers and lets the
 * caller decide what to render.
 */

import { apiBase, withAuth, wsUrl } from '$lib/api/runtime';
import { browser } from '$app/environment';
import { SttTranscript } from './sttTranscript';

// ============================================================================
// Types
// ============================================================================

export interface SttCapability {
	supported: boolean;
	model: string;
	model_name: string;
	/** False for whisper-1: it emits whole segments, never mid-utterance deltas. */
	partials: boolean;
	sample_rate: number;
	reason: string | null;
	/** Whether this model accepts the browser's native recording container, so
	 *  the batch path can upload Opus instead of re-encoding to 16 kHz WAV. */
	accepts_compressed?: boolean;
	/** Live-only models cannot recover through the recorded-file endpoint. */
	realtime_only?: boolean;
}

export interface SttStreamHandlers {
	/** Full reconciled dictation, including all settled and in-flight turns. */
	onTranscript?: (text: string) => void;
	/** The upstream accepted the model configuration. Microphone acquisition
	 *  runs concurrently; use `onCapture` for the recording-start signal. */
	onReady?: (info: { model: string; modelName: string; partials: boolean; serverVad?: boolean }) => void;
	/** The audio graph is connected and samples are flowing. This is the only
	 *  honest "you can talk now" signal, and the one the UI and earcon fire on. */
	onCapture?: () => void;
	/** More text for the segment currently being spoken. */
	onDelta?: (text: string) => void;
	/** A segment finished; `text` is its settled transcript. */
	onFinal?: (text: string) => void;
	/** Speech changed, from server VAD or local PCM when the model has no VAD. */
	onSpeech?: (active: boolean) => void;
	/** Per-frame audio energy for the meter: `level` is 0–1 RMS, `bands` a
	 *  low-to-high spectrum slice already normalised to 0–1. */
	onAudio?: (level: number, bands: Float32Array) => void;
	onError?: (message: string, fatal: boolean) => void;
	onClosed?: () => void;
}

export interface SttStreamSession {
	/** Finish cleanly: stop the mic, flush the tail segment, wait for its
	 *  transcript. Resolves with everything transcribed during the session. */
	stop: () => Promise<string>;
	/** Tear down now, discarding anything still in flight. */
	abort: () => void;
	/** Text finalised so far, joined. */
	getText: () => string;
}

/** Thrown when the socket refuses the session because the configured model
 *  cannot stream — the caller should fall back to the batch upload path. */
export class SttUnsupportedError extends Error {
	constructor(message: string) {
		super(message);
		this.name = 'SttUnsupportedError';
	}
}

// ============================================================================
// Constants
// ============================================================================

const TARGET_SAMPLE_RATE = 24000;

/** Frames per outbound chunk. 2048 @ 24 kHz is ~85 ms — under the 100 ms the
 *  realtime API prefers, and far fewer sends than the worklet's native 128. */
const CHUNK_FRAMES = 2048;

/** Spectrum buckets for the visualiser. */
const BAND_COUNT = 28;

/** Stop sending rather than queue behind a stalled socket. Audio the user has
 *  already spoken is worth less than staying in sync with what they say next. */
const MAX_BUFFERED_BYTES = 1_000_000;

/** How long `stop()` waits for the tail segment's transcript before giving up. */
const DRAIN_TIMEOUT_MS = 7000;

const CLOSE_UNSUPPORTED = 4003;
const CLOSE_NO_CREDENTIAL = 4004;

// The chunker runs on the audio thread and does one job: accumulate frames into
// fixed-size blocks and hand them over, transferring the buffer so nothing is
// copied. Shipped as source text and loaded from a blob URL — it needs no build
// step and no static asset that could drift out of sync with this file.
const WORKLET_SOURCE = `
class PcmChunker extends AudioWorkletProcessor {
	constructor() {
		super();
		this._size = ${CHUNK_FRAMES};
		this._buf = new Float32Array(this._size);
		this._n = 0;
	}
	process(inputs) {
		const channel = inputs[0] && inputs[0][0];
		if (!channel) return true;
		for (let i = 0; i < channel.length; i++) {
			this._buf[this._n++] = channel[i];
			if (this._n === this._size) {
				const out = this._buf.slice(0);
				this.port.postMessage(out, [out.buffer]);
				this._n = 0;
			}
		}
		return true;
	}
}
registerProcessor('pcm-chunker', PcmChunker);
`;

// ============================================================================
// Capability probe
// ============================================================================

let capabilityCache: { at: number; value: SttCapability } | null = null;

// Long, because the cache is invalidated on the events that actually change the
// answer: both settings call sites call `invalidateSttCapability`, and a 4003 /
// 4004 close does the same for a model changed on another device. A short TTL
// only bought a round trip on the critical path of nearly every press.
const CAPABILITY_TTL_MS = 5 * 60_000;

/**
 * Ask the backend whether the configured STT model can stream. Cached briefly
 * so a push-to-talk hotkey doesn't re-probe on every press.
 */
export async function fetchSttCapability(force = false): Promise<SttCapability> {
	if (!force && capabilityCache && Date.now() - capabilityCache.at < CAPABILITY_TTL_MS) {
		return capabilityCache.value;
	}

	const response = await fetch(`${apiBase()}/settings/transcribe/stream/capability`, withAuth({ }));
	if (!response.ok) {
		throw new Error(`Capability check failed (${response.status})`);
	}
	const value = (await response.json()) as SttCapability;
	capabilityCache = { at: Date.now(), value };
	return value;
}

/** Drop the cached probe — call after the STT model setting changes. */
export function invalidateSttCapability(): void {
	capabilityCache = null;
}

// ============================================================================
// Audio helpers
// ============================================================================

/** Linear resample. Only runs when the browser refused a 24 kHz AudioContext. */
function resample(input: Float32Array, from: number, to: number): Float32Array {
	if (from === to) return input;
	const ratio = from / to;
	const length = Math.floor(input.length / ratio);
	const output = new Float32Array(length);
	for (let i = 0; i < length; i++) {
		const position = i * ratio;
		const index = Math.floor(position);
		const frac = position - index;
		const a = input[index] ?? 0;
		const b = input[index + 1] ?? a;
		output[i] = a + (b - a) * frac;
	}
	return output;
}

function floatToPcm16(input: Float32Array): ArrayBuffer {
	const output = new Int16Array(input.length);
	for (let i = 0; i < input.length; i++) {
		const sample = Math.max(-1, Math.min(1, input[i]));
		output[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
	}
	return output.buffer;
}

/** Turn a getUserMedia rejection into something worth showing a user. */
function describeMicError(error: unknown): Error {
	const err = error as { name?: string; message?: string };
	if (err?.name === 'NotAllowedError') {
		return new Error(
			'Microphone access denied. Please allow microphone access in your browser settings.'
		);
	}
	if (err?.name === 'NotFoundError') {
		return new Error('No microphone found. Please connect a microphone and try again.');
	}
	return error instanceof Error ? error : new Error(String(err?.message || error));
}

function websocketUrl(language?: string): string {
	return wsUrl('/api/v1/settings/transcribe/stream', { language });
}

// ============================================================================
// Session
// ============================================================================

/**
 * Open a live transcription session. Resolves once the socket is ready and the
 * microphone is streaming; rejects with `SttUnsupportedError` when the backend
 * says this model can only do batch transcription.
 */
export async function startSttStream(
	handlers: SttStreamHandlers = {},
	language?: string
): Promise<SttStreamSession> {
	if (!browser) throw new Error('Live transcription requires a browser.');
	if (!navigator.mediaDevices) {
		throw new Error(
			'Microphone requires a secure context (HTTPS or localhost). In Chrome, you can allow this origin at chrome://flags/#unsafely-treat-insecure-origin-as-secure'
		);
	}

	const transcript = new SttTranscript();
	let pending = '';
	let closed = false;
	let fatalError: string | null = null;
	let clientSpeechDetection = false;
	let localSpeechActive = false;


	let stream: MediaStream | null = null;
	let audioContext: AudioContext | null = null;
	let sourceNode: MediaStreamAudioSourceNode | null = null;
	let workletNode: AudioWorkletNode | null = null;
	let processorNode: ScriptProcessorNode | null = null;
	let analyser: AnalyserNode | null = null;
	let levelFrame: number | null = null;
	let socket: WebSocket | null = null;

	let resolveClosed: (() => void) | null = null;
	const closedPromise = new Promise<void>((resolve) => {
		resolveClosed = resolve;
	});

	function teardownAudio(): void {
		if (levelFrame !== null) {
			cancelAnimationFrame(levelFrame);
			levelFrame = null;
		}
		try {
			workletNode?.disconnect();
			workletNode?.port.close();
		} catch {
			/* already gone */
		}
		try {
			processorNode?.disconnect();
		} catch {
			/* already gone */
		}
		try {
			sourceNode?.disconnect();
			analyser?.disconnect();
		} catch {
			/* already gone */
		}
		stream?.getTracks().forEach((track) => track.stop());
		audioContext?.close().catch(() => {});

		workletNode = null;
		processorNode = null;
		sourceNode = null;
		analyser = null;
		stream = null;
		audioContext = null;
	}

	function finish(): void {
		if (closed) {
			teardownAudio();
			return;
		}
		closed = true;
		teardownAudio();
		try {
			socket?.close();
		} catch {
			/* already closing */
		}
		socket = null;
		handlers.onClosed?.();
		resolveClosed?.();
	}

	// ── Socket ────────────────────────────────────────────────────────────

	socket = new WebSocket(websocketUrl(language));
	socket.binaryType = 'arraybuffer';

	const ready = new Promise<void>((resolve, reject) => {
		let settled = false;

		socket!.onmessage = (event) => {
			if (closed || typeof event.data !== 'string') return;
			let message: Record<string, unknown>;
			try {
				message = JSON.parse(event.data);
			} catch {
				return;
			}
			if (!message || typeof message !== 'object' || Array.isArray(message)) return;

			switch (message.type) {
				case 'item_started':
					transcript.start(
						typeof message.item_id === 'string' ? message.item_id : undefined,
						message.previous_item_id === null ? null : typeof message.previous_item_id === 'string' ? message.previous_item_id : undefined
					);
					break;
				case 'ready':
					if (!settled) {
						settled = true;
						clientSpeechDetection = message.server_vad === false;
						handlers.onReady?.({
							model: String(message.model ?? ''),
							modelName: String(message.model_name ?? ''),
							partials: Boolean(message.partials),
							serverVad: !clientSpeechDetection
						});
						resolve();
					}
					break;

				case 'delta':
					pending += String(message.text ?? '');
					transcript.delta(String(message.text ?? ''), typeof message.item_id === 'string' ? message.item_id : undefined);
					handlers.onTranscript?.(transcript.getText());
					handlers.onDelta?.(pending);
					break;

				case 'final': {
					const text = String(message.text ?? '').trim();
					pending = '';
					transcript.final(text, typeof message.item_id === 'string' ? message.item_id : undefined);
					handlers.onTranscript?.(transcript.getText());
					if (text) {
						handlers.onFinal?.(text);
					} else {
						// An empty final still clears the partial the UI is showing.
						handlers.onDelta?.('');
					}
					break;
				}

				case 'speech_started':
					handlers.onSpeech?.(true);
					break;

				case 'speech_stopped':
					handlers.onSpeech?.(false);
					break;

				case 'error': {
					const detail = String(message.message ?? 'Live transcription failed.');
					if (!settled && message.unsupported) {
						settled = true;
						reject(new SttUnsupportedError(detail));
						return;
					}
					if (message.fatal) fatalError = detail;
					handlers.onError?.(detail, Boolean(message.fatal));
					if (!settled && message.fatal) {
						settled = true;
						reject(new Error(detail));
					}
					if (message.fatal) finish();
					break;
				}

				case 'closed':
					if (!settled) {
						settled = true;
						reject(new Error(fatalError || 'Live transcription stream closed.'));
					}
					finish();
					break;
			}
		};

		socket!.onerror = () => {
			if (!settled) {
				settled = true;
				reject(new Error('Could not open the live transcription stream.'));
			}
		};

		socket!.onclose = (event) => {
			if (event.code === CLOSE_UNSUPPORTED || event.code === CLOSE_NO_CREDENTIAL) {
				// The cached probe disagrees with the server — most likely the STT
				// model was changed on another device. Re-probe next press.
				invalidateSttCapability();
			}
			if (!settled) {
				settled = true;
				const detail =
					event.code === CLOSE_UNSUPPORTED || event.code === CLOSE_NO_CREDENTIAL
						? new SttUnsupportedError(
								event.reason || 'Live transcription is not available for this model.'
							)
						: new Error(event.reason || 'Live transcription stream closed.');
				reject(detail);
			}
			finish();
		};
	});

	// ── Microphone ────────────────────────────────────────────────────────

	// Opened concurrently with the handshake, not after it. Nothing about
	// getUserMedia depends on the socket, and serialising them meant the user
	// waited out a capability check, a WebSocket accept and a full TLS connect
	// to OpenAI before the microphone was even requested — dead time they
	// routinely spent talking.
	const micPromise = navigator.mediaDevices.getUserMedia({
		audio: {
			channelCount: 1,
			echoCancellation: true,
			noiseSuppression: true,
			autoGainControl: true
		}
	}).then((opened) => {
		// A permission prompt may outlive a rejected model session. Its result
		// remains owned here even after startSttStream has already rejected.
		if (closed) opened.getTracks().forEach((track) => track.stop());
		else stream = opened;
		return opened;
	});

	try {
		const [, opened] = await Promise.race([
			Promise.all([ready, micPromise]),
			closedPromise.then(() => { throw new Error(fatalError || 'Live transcription stream closed.'); })
		]);
		if (closed) throw new Error(fatalError || 'Live transcription stopped before capture began.');
		stream = opened;
		// Pinning the context to 24 kHz makes the browser resample in native
		// code. Not every engine honours it, hence the rate check below.
		try {
			audioContext = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
		} catch {
			audioContext = new AudioContext();
		}
		if (audioContext.state === 'suspended') {
			await audioContext.resume();
		}
		if (closed) throw new Error('Live transcription stopped before capture began.');

		const contextRate = audioContext.sampleRate;
		sourceNode = audioContext.createMediaStreamSource(stream);

		analyser = audioContext.createAnalyser();
		analyser.fftSize = 256;
		analyser.smoothingTimeConstant = 0.72;
		sourceNode.connect(analyser);

		const sendChunk = (frames: Float32Array) => {
			if (closed || !audioContext) return;
			// The capture callback keeps running when animation frames pause.
			// GPT Live has no server VAD: detect speech from unsmoothed PCM so
			// silence can end the recording and commit its final turn.
			if (clientSpeechDetection && frames.length) {
				let sumSquares = 0;
				for (const sample of frames) sumSquares += sample * sample;
				const active = Math.sqrt(sumSquares / frames.length) >= 0.02;
				if (active !== localSpeechActive) {
					localSpeechActive = active;
					handlers.onSpeech?.(active);
				}
			}
			if (!socket || socket.readyState !== WebSocket.OPEN) return;
			if (socket.bufferedAmount > MAX_BUFFERED_BYTES) return;
			const resampled = resample(frames, contextRate, TARGET_SAMPLE_RATE);
			socket.send(floatToPcm16(resampled));
		};

		let usingWorklet = false;
		if (audioContext.audioWorklet) {
			try {
				const blobUrl = URL.createObjectURL(
					new Blob([WORKLET_SOURCE], { type: 'application/javascript' })
				);
				await audioContext.audioWorklet.addModule(blobUrl);
				URL.revokeObjectURL(blobUrl);
				if (closed) throw new Error('Live transcription stopped before capture began.');

				workletNode = new AudioWorkletNode(audioContext, 'pcm-chunker', {
					numberOfInputs: 1,
					numberOfOutputs: 0,
					channelCount: 1
				});
				workletNode.port.onmessage = (event) => sendChunk(event.data as Float32Array);
				sourceNode.connect(workletNode);
				usingWorklet = true;
			} catch (workletError) {
				if (closed) throw new Error(fatalError || 'Live transcription stopped before capture began.');
				console.warn('[SttStream] AudioWorklet unavailable, falling back:', workletError);
				workletNode = null;
			}
		}

		if (!usingWorklet) {
			processorNode = audioContext.createScriptProcessor(CHUNK_FRAMES, 1, 1);
			processorNode.onaudioprocess = (event) => {
				// The event buffer is reused between callbacks — copy before it
				// reaches the resampler, which outlives this tick.
				sendChunk(new Float32Array(event.inputBuffer.getChannelData(0)));
			};
			sourceNode.connect(processorNode);
			// ScriptProcessorNode only fires while it reaches the destination.
			// A zero-gain sink keeps it running without echoing the mic.
			const sink = audioContext.createGain();
			sink.gain.value = 0;
			processorNode.connect(sink);
			sink.connect(audioContext.destination);
		}

		// ── Meter ─────────────────────────────────────────────────────────
		const spectrum = new Uint8Array(analyser.frequencyBinCount);
		const bands = new Float32Array(BAND_COUNT);
		const perBand = Math.floor(spectrum.length / BAND_COUNT) || 1;

		const tick = () => {
			if (!analyser) return;
			analyser.getByteFrequencyData(spectrum);

			let sumSquares = 0;
			for (let band = 0; band < BAND_COUNT; band++) {
				let total = 0;
				for (let i = 0; i < perBand; i++) {
					total += spectrum[band * perBand + i] ?? 0;
				}
				const value = total / perBand / 255;
				bands[band] = value;
				sumSquares += value * value;
			}
			handlers.onAudio?.(Math.sqrt(sumSquares / BAND_COUNT), bands);
			levelFrame = requestAnimationFrame(tick);
		};
		levelFrame = requestAnimationFrame(tick);

		// Capture is genuinely live now: the graph is connected on both the
		// worklet and the ScriptProcessor branch, and the meter is running.
		// Announced here rather than on the socket's `ready` — that arrives
		// while getUserMedia or the audio worklet may still be starting.
		handlers.onCapture?.();
	} catch (error) {
		finish();
		throw describeMicError(error);
	}

	// ── Control ───────────────────────────────────────────────────────────

	// Keep provisional words if the final misses the bounded drain window.
	// Each final replaces its item by identity, so it never doubles the text.
	const getText = () => transcript.getText();

	return {
		getText,

		async stop() {
			if (closed) {
				if (fatalError && !getText()) throw new Error(fatalError);
				return getText();
			}

			// Silence the microphone first so the tail commit covers exactly what
			// was spoken, then let the backend drain the last segment.
			teardownAudio();

			if (socket && socket.readyState === WebSocket.OPEN) {
				try {
					socket.send(JSON.stringify({ type: 'stop' }));
				} catch {
					/* socket already going away */
				}
				// The proxy drains every in-flight turn. The first final may belong
				// to an earlier turn, so closing on it would drop the tail words.
				await Promise.race([
					closedPromise,
					new Promise<void>((resolve) => setTimeout(resolve, DRAIN_TIMEOUT_MS))
				]);
			}

			finish();
			const text = getText();
			if (fatalError && !text) throw new Error(fatalError);
			return text;
		},

		abort() {
			finish();
		}
	};
}
