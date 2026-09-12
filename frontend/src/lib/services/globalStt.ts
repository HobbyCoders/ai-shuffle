/**
 * Global STT Service
 *
 * Voice input on a configurable hotkey. Two paths, chosen per press:
 *
 * - **Live** (OpenAI STT models): audio streams to the backend proxy and text
 *   comes back while the user is still talking. See `sttStream`.
 * - **Batch** (fal models, or when live is unavailable): record the whole clip,
 *   re-encode to WAV, upload it, wait for one answer.
 *
 * Either way the finished text lands on the clipboard and, if something is
 * focused, at the caret.
 */

import { writable, get } from 'svelte/store';
import { apiBase, authHeaders, requestCredentials } from '$lib/api/runtime';
import { browser } from '$app/environment';
import { loadVoiceSettings } from '$lib/api/voice';
import { registerShortcut, type ShortcutRegistration } from './keyboard';
import { hotkeyPreferences, type HotkeyConfig } from '$lib/stores/hotkeyPreferences.svelte';
import { convertToWav } from '$lib/utils/audioConvert';
import { playEarcon, primeEarcons, setEarconsEnabled } from '$lib/utils/earcon';
import {
	fetchSttCapability,
	startSttStream,
	SttUnsupportedError,
	type SttStreamSession
} from './sttStream';

// ============================================================================
// Types
// ============================================================================

export type GlobalSttMode = 'idle' | 'live' | 'batch';

/**
 * Where dictation goes while it is still being spoken.
 *
 * The composer registers one of these so live text lands in the editor as the
 * user talks, instead of arriving in a lump after they stop. `onText` gets the
 * FULL dictation so far every time — the consumer replaces its span rather than
 * appending, which is what lets the model revise a word mid-sentence.
 */
export interface VoiceInputSink {
	onText: (text: string) => void;
	onCommit: () => void;
	onDiscard?: () => void;
}

export interface GlobalSttState {
	/** Which surface started this dictation and should display its controls. */
	source: 'global' | 'composer';
	isRecording: boolean;
	isTranscribing: boolean;
	isPreparing: boolean;
	audioLevel: number;
	error: string | null;
	lastTranscription: string | null;
	/** Which path this press took. */
	mode: GlobalSttMode;
	/** Live only: the segment being spoken right now, still growing. */
	partial: string;
	/** Live only: segments the model has settled on, in order. */
	segments: string[];
	/** Live only: server VAD or local PCM detection currently hears speech. */
	speechActive: boolean;
	/** Live only: the model doing the transcribing, for the overlay caption. */
	modelName: string | null;
	/** Live only: false for whisper-1, which returns whole segments with no
	 *  mid-utterance deltas — the overlay explains the wait instead of
	 *  pretending nothing is happening. */
	partialsSupported: boolean;
}

/** Meter data, kept out of the main store so a 60 fps waveform never
 *  re-renders the transcript. */
export interface GlobalSttAudio {
	level: number;
	bands: number[];
}

// ============================================================================
// State Stores
// ============================================================================

const initialState: GlobalSttState = {
	source: 'global',
	isRecording: false,
	isTranscribing: false,
	isPreparing: false,
	audioLevel: 0,
	error: null,
	lastTranscription: null,
	mode: 'idle',
	partial: '',
	segments: [],
	speechActive: false,
	modelName: null,
	partialsSupported: true
};

export const globalSttState = writable<GlobalSttState>(initialState);

const BAND_COUNT = 28;
const emptyBands = () => new Array(BAND_COUNT).fill(0);

export const globalSttAudio = writable<GlobalSttAudio>({ level: 0, bands: emptyBands() });

/** Batch upload progress, 0-1, or null when nothing is uploading. Rendered as a
 *  fill inside the microphone button — the upload is the leg that actually takes
 *  time on a phone, and it is the only part of the wait we can report honestly. */
export const globalSttUpload = writable<number | null>(null);

// ============================================================================
// Private State
// ============================================================================

let currentRegistration: ShortcutRegistration | null = null;
let mediaRecorder: MediaRecorder | null = null;
let mediaStream: MediaStream | null = null;
let audioChunks: Blob[] = [];
let recordingStartTime: number = 0;

// Live session handle (null while on the batch path)
let streamSession: SttStreamSession | null = null;

// Audio level monitoring (batch path only — the live path meters in sttStream)
let audioContext: AudioContext | null = null;
let analyserNode: AnalyserNode | null = null;
let audioMonitorTimer: ReturnType<typeof setInterval> | null = null;

const MIN_RECORDING_DURATION = 500; // 500ms minimum

// Stop on our own once the speaker has clearly finished. Without this the only
// way out is the cancel affordance, which throws the transcript away — so the
// natural gesture (stop talking) had no natural outcome.
//
// Server VAD has already waited 700ms before reporting silence. Batch and
// live models without VAD detect silence locally and need the full five seconds.
// Snapshot preferences per recording. A settings refresh must not change a
// countdown mid-utterance. Global hotkey dictation keeps its existing behavior.
let inputMode: 'auto' | 'toggle' = 'auto';
let silenceStopMs = 5000;

// A session where nothing is ever said should not hold the microphone open
// indefinitely either.
const NO_SPEECH_AUTO_STOP_MS = 12000;

// Batch detection threshold. sttStream uses the same PCM threshold only for
// live models without server VAD.
const SILENCE_RMS_THRESHOLD = 0.02;

// Mutex to prevent race conditions during toggle
let isToggling = false;

// Bumped on every start and on every cancel. Async continuations snapshot it and
// bail if it has moved on: resetting the store alone would not stop an in-flight
// stop handler from inserting the text the user just discarded.
let runId = 0;
const isCurrentRun = (id: number) => id === runId;

// The batch upload in flight, so cancel can abort it instead of paying for a
// transcription nobody will read.
let uploadRequest: XMLHttpRequest | null = null;

// Where finished text goes. Null means the default: clipboard, plus the caret
// if something is focused. The composer's microphone button sets a sink so its
// text lands in the editor even though focus is on the button.
let outputSink: ((text: string) => void) | null = null;

// Where in-progress dictation goes. Set alongside outputSink by a caller that
// can render text as it arrives; when it is present the final text has already
// been written, so outputSink is not called again.
let liveSink: VoiceInputSink | null = null;

function settleSink(discard = false): void {
	const sink = liveSink;
	liveSink = null;
	outputSink = null;
	try {
		if (discard) sink?.onDiscard?.();
		else sink?.onCommit();
	} catch (error) {
		console.error('[GlobalSTT] Could not settle dictation draft:', error);
	}
}

// Auto-stop timers.
let silenceTimer: ReturnType<typeof setTimeout> | null = null;
let noSpeechTimer: ReturnType<typeof setTimeout> | null = null;

function clearAutoStop(): void {
	if (silenceTimer !== null) {
		clearTimeout(silenceTimer);
		silenceTimer = null;
	}
	if (noSpeechTimer !== null) {
		clearTimeout(noSpeechTimer);
		noSpeechTimer = null;
	}
}

/** Stop because the speaker has finished, not because they asked. */
function autoStop(): void {
	clearAutoStop();
	if (!get(globalSttState).isRecording) return;
	void stopRecording();
}

/** Arm (or re-arm) the end-of-speech countdown. */
function armSilenceStop(delayMs: number): void {
	if (inputMode === 'toggle') return;
	if (silenceTimer !== null) clearTimeout(silenceTimer);
	silenceTimer = setTimeout(autoStop, delayMs);
}

function cancelSilenceStop(): void {
	if (silenceTimer !== null) {
		clearTimeout(silenceTimer);
		silenceTimer = null;
	}
	if (noSpeechTimer !== null) {
		clearTimeout(noSpeechTimer);
		noSpeechTimer = null;
	}
}

// Track current MIME type for proper file extension
let currentMimeType: string = 'audio/webm';

// Whether the configured STT model accepts the browser's own recording
// container. Defaults to false so an unknown or unreachable model still gets the
// safe (converted) upload.
let sttAcceptsCompressed = false;

// ============================================================================
// Audio Helpers
// ============================================================================

/**
 * Get the best supported audio MIME type for recording
 */
function getSupportedMimeType(): string {
	if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) {
		return 'audio/webm;codecs=opus';
	}
	if (MediaRecorder.isTypeSupported('audio/webm')) {
		return 'audio/webm';
	}
	return 'audio/mp4';
}

/**
 * Get file extension for a MIME type
 */
function getFileExtension(mimeType: string): string {
	if (mimeType.includes('mp4')) return 'mp4';
	if (mimeType.includes('ogg')) return 'ogg';
	return 'webm';
}

// ============================================================================
// Audio Level Monitoring
// ============================================================================

// Whether this batch recording has heard anything yet. The silence countdown
// only starts after real speech, so a slow starter is not cut off before they
// have said a word.
let heardSpeech = false;

async function initAudioMonitoring(stream: MediaStream, myRun: number): Promise<() => void> {
	heardSpeech = false;
	const context = new AudioContext();
	audioContext = context;
	if (context.state === 'suspended') await context.resume();
	if (!isCurrentRun(myRun)) return () => {};
	analyserNode = context.createAnalyser();
	analyserNode.fftSize = 256;
	analyserNode.smoothingTimeConstant = 0.8;

	const source = context.createMediaStreamSource(stream);
	source.connect(analyserNode);

	const dataArray = new Uint8Array(analyserNode.frequencyBinCount);
	const samples = new Float32Array(analyserNode.fftSize);
	const bands = emptyBands();
	const perBand = Math.floor(dataArray.length / BAND_COUNT) || 1;

	function updateLevel() {
		if (!analyserNode || !isCurrentRun(myRun)) return;

		analyserNode.getByteFrequencyData(dataArray);
		analyserNode.getFloatTimeDomainData(samples);

		// Measure the waveform itself. Smoothed frequency bins are useful for
		// drawing bars, but can keep reporting energy after speech has ended.
		let sum = 0;
		for (const sample of samples) {
			sum += sample * sample;
		}
		const rms = Math.sqrt(sum / samples.length);

		// Same band shape the live path publishes, so one waveform component
		// drives both modes.
		for (let band = 0; band < BAND_COUNT; band++) {
			let total = 0;
			for (let i = 0; i < perBand; i++) {
				total += dataArray[band * perBand + i] ?? 0;
			}
			bands[band] = total / perBand / 255;
		}

		globalSttState.update((s) => ({
			...s,
			audioLevel: s.audioLevel * 0.7 + rms * 0.3
		}));
		globalSttAudio.set({ level: rms, bands: [...bands] });

		// The batch path has no server VAD, so end-of-speech is inferred from
		// loudness. Crude next to real VAD, but it gives this path the same
		// "stop talking and it finishes" behaviour.
		if (rms >= SILENCE_RMS_THRESHOLD) {
			heardSpeech = true;
			cancelSilenceStop();
		} else if (heardSpeech && silenceTimer === null) {
			armSilenceStop(silenceStopMs);
		}
	}

	// Speech detection must keep running when animation frames are paused (for
	// example, in a background tab). Do not tie recording lifetime to rendering.
	return () => {
		if (audioMonitorTimer === null) {
			audioMonitorTimer = setInterval(updateLevel, 50);
		}
	};
}

function stopAudioMonitoring(): void {
	if (audioMonitorTimer !== null) {
		clearInterval(audioMonitorTimer);
		audioMonitorTimer = null;
	}

	if (audioContext) {
		audioContext.close().catch(() => {});
		audioContext = null;
	}

	analyserNode = null;

	globalSttState.update((s) => ({ ...s, audioLevel: 0 }));
	globalSttAudio.set({ level: 0, bands: emptyBands() });
}

// ============================================================================
// Live Recording (streaming)
// ============================================================================

/** Everything transcribed so far, settled segments plus the in-flight one. */
function dictationText(state: GlobalSttState): string {
	return [...state.segments, state.partial].join(' ').replace(/\s+/g, ' ').trim();
}

/** Mirror the running dictation into whatever surface registered for it. */
function pushLiveText(state: GlobalSttState): void {
	if (!liveSink) return;
	try {
		liveSink.onText(dictationText(state));
	} catch (error) {
		// A consumer that throws (unmounted editor, detached node) must not take
		// the recording down with it.
		console.warn('[GlobalSTT] Live sink failed:', error);
		settleSink();
	}
}

/**
 * Try the live path. Returns false when this setup can only do batch, so the
 * caller falls through without the user seeing a failure.
 */
async function startLiveRecording(myRun: number): Promise<boolean> {
	let capability;
	try {
		capability = await fetchSttCapability();
	} catch (error) {
		console.warn('[GlobalSTT] Capability probe failed, using batch path:', error);
		sttAcceptsCompressed = false;
		return false;
	}
	if (!isCurrentRun(myRun)) return true;

	// Recorded before the early return: the batch path below reads it, and an
	// unsupported model is exactly the case that falls through to batch.
	sttAcceptsCompressed = capability.accepts_compressed === true;

	if (!capability.supported) {
		if (!capability.realtime_only) return false;
		settleSink();
		globalSttState.update((s) => ({ ...s, mode: 'idle', isPreparing: false,
			isRecording: false, error: capability.reason || 'This model requires live transcription.' }));
		return true;
	}

	globalSttState.update((s) => ({
		...s,
		mode: 'live',
		partial: '',
		segments: [],
		speechActive: false,
		modelName: capability.model_name,
		partialsSupported: capability.partials
	}));

	let liveSilenceStopMs = Math.max(0, silenceStopMs - 700);
	try {
		const session = await startSttStream({
			onReady: (info) => {
				if (!isCurrentRun(myRun)) return;
				// Use the accepted session, since a setting may change after the
				// capability probe. Readiness alone does not start the microphone UI.
				liveSilenceStopMs = info.serverVad === false
					? silenceStopMs : Math.max(0, silenceStopMs - 700);
			},
			onCapture: () => {
				if (!isCurrentRun(myRun)) return;
				globalSttState.update((s) => ({
					...s,
					isPreparing: false,
					isRecording: true
				}));
				playEarcon('ready');
				// Nothing said at all still ends the session eventually.
				if (inputMode === 'auto') noSpeechTimer = setTimeout(autoStop, NO_SPEECH_AUTO_STOP_MS);
			},
			onClosed: () => {
				if (!isCurrentRun(myRun)) return;
				// The socket died on its own. Guard on streamSession rather than
				// isRecording: stopLiveRecording nulls it synchronously, so the
				// normal stop path cannot recurse through here.
				if (streamSession) void stopLiveRecording('dropped');
			},
			onTranscript: (text) => {
				if (!isCurrentRun(myRun)) return;
				globalSttState.update((s) => {
					const next = { ...s, partial: text, segments: [] };
					pushLiveText(next);
					return next;
				});
			},
			onSpeech: (active) => {
				if (!isCurrentRun(myRun) || !get(globalSttState).isRecording) return;
				globalSttState.update((s) => ({ ...s, speechActive: active }));
				// sttStream uses local PCM only when this model has no server VAD.
				if (active) {
					cancelSilenceStop();
				} else {
					armSilenceStop(liveSilenceStopMs);
				}
			},
			onAudio: (level, bands) => {
				if (!isCurrentRun(myRun)) return;
				globalSttAudio.set({ level, bands: Array.from(bands) });
			},
			onError: (message, fatal) => {
				if (!isCurrentRun(myRun)) return;
				if (fatal) {
					globalSttState.update((s) => ({ ...s, error: message }));
				} else {
					console.warn('[GlobalSTT] Live transcription warning:', message);
				}
			}
		});
		if (!isCurrentRun(myRun)) session.abort();
		else streamSession = session;
		return true;
	} catch (error) {
		if (!isCurrentRun(myRun)) return true;
		streamSession = null;

		if (error instanceof SttUnsupportedError && !capability.realtime_only) {
			// The backend says this model can't stream — batch handles it.
			globalSttState.update((s) => ({ ...s, mode: 'idle' }));
			return false;
		}

		// A real failure (microphone denied, socket refused). Surfacing it beats
		// silently retrying on the batch path, which would fail the same way and
		// prompt for the microphone a second time.
		const err = error as { message?: string };
		globalSttState.update((s) => ({
			...s,
			mode: 'idle',
			isPreparing: false,
			isRecording: false,
			error: err.message || 'Live transcription failed to start'
		}));
		return true;
	}
}

async function stopLiveRecording(reason: 'user' | 'dropped' = 'user'): Promise<void> {
	const session = streamSession;
	streamSession = null;
	if (!session) {
		settleSink();
		globalSttState.update((s) => ({ ...s, isRecording: false, mode: 'idle' }));
		return;
	}

	clearAutoStop();
	playEarcon('stop');

	// Drain: the tail segment is still transcribing upstream.
	globalSttState.update((s) => ({
		...s,
		isRecording: false,
		isTranscribing: true,
		speechActive: false,
		// Never clobber a message the backend already sent (a length or byte cap
		// explains the drop far better than a generic disconnect notice would).
		error: reason === 'dropped' ? s.error ?? 'Live transcription disconnected.' : s.error
	}));
	globalSttAudio.set({ level: 0, bands: emptyBands() });

	const myRun = runId;

	try {
		const text = (await session.stop()).trim();
		if (!isCurrentRun(myRun)) return;
		if (text && liveSink) {
			// The sink has been rendering this all along; the last update just
			// settles it. Routing it through outputText as well would insert the
			// whole transcript a second time.
			liveSink.onText(text);
			globalSttState.update((s) => ({ ...s, lastTranscription: text }));
		} else if (text) {
			await outputText(text, myRun);
			globalSttState.update((s) => ({ ...s, lastTranscription: text }));
		} else if (reason === 'user') {
			// On a drop the disconnect message is the useful one; "no speech"
			// would blame the user for the network.
			globalSttState.update((s) => ({ ...s, error: 'No speech detected' }));
			playEarcon('error');
		}
	} catch (error) {
		if (!isCurrentRun(myRun)) return;
		const err = error as { message?: string };
		globalSttState.update((s) => ({
			...s,
			error: err.message || 'Live transcription failed'
		}));
		playEarcon('error');
	} finally {
		if (isCurrentRun(myRun)) {
			settleSink();
			globalSttState.update((s) => ({
				...s,
				isTranscribing: false,
				mode: 'idle',
				partial: '',
				segments: [],
				speechActive: false
			}));
		}
	}
}

// ============================================================================
// Recording Functions
// ============================================================================

async function startRecording(source: GlobalSttState['source']): Promise<void> {
	const state = get(globalSttState);

	// Don't start if already recording or transcribing
	if (state.isRecording || state.isTranscribing || state.isPreparing) {
		return;
	}

	audioChunks = [];
	const myRun = ++runId;
	if (source === 'global') {
		outputSink = null;
		liveSink = null;
	}

	globalSttState.update((s) => ({
		...s,
		source,
		isPreparing: true,
		error: null,
		partial: '',
		segments: [],
		lastTranscription: null
	}));
	globalSttUpload.set(null);
	recordingStartTime = Date.now();

	inputMode = 'auto';
	silenceStopMs = 5000;
	if (source === 'composer') {
		try {
			const settings = await loadVoiceSettings();
			if (!isCurrentRun(myRun)) return;
			inputMode = settings.voice_input_mode === 'toggle' ? 'toggle' : 'auto';
			const seconds = settings.voice_input_silence_seconds ?? 5;
			silenceStopMs = (Number.isInteger(seconds) && seconds >= 1 && seconds <= 60 ? seconds : 5) * 1000;
		} catch {
			if (isCurrentRun(myRun)) {
				settleSink();
				globalSttState.update((s) => ({ ...s, isPreparing: false,
					error: 'Could not load voice input settings. Try again.' }));
			}
			return;
		}
	}

	// Live first; batch is the fallback for models that can't stream.
	if (await startLiveRecording(myRun)) {
		if (isCurrentRun(myRun) && !get(globalSttState).isRecording) settleSink();
		return;
	}
	if (!isCurrentRun(myRun)) return;

	globalSttState.update((s) => ({ ...s, mode: 'batch' }));

	try {
		// Get microphone access - check for secure context (HTTPS or localhost)
		if (!navigator.mediaDevices) {
			throw new Error(
				'Microphone requires a secure context (HTTPS or localhost). In Chrome, you can allow this origin at chrome://flags/#unsafely-treat-insecure-origin-as-secure'
			);
		}
		const stream = await navigator.mediaDevices.getUserMedia({
			audio: {
				echoCancellation: true,
				noiseSuppression: true,
				autoGainControl: true
			}
		});
		if (!isCurrentRun(myRun)) {
			stream.getTracks().forEach((track) => track.stop());
			return;
		}
		mediaStream = stream;

		// Determine best supported mime type
		currentMimeType = getSupportedMimeType();

		const recorder = new MediaRecorder(mediaStream, { mimeType: currentMimeType });

		// Collect audio chunks
		recorder.ondataavailable = (event) => {
			if (isCurrentRun(myRun) && event.data.size > 0) {
				audioChunks.push(event.data);
			}
		};

		// Start recording with timeslice for chunked data collection
		recorder.start(500); // 500ms chunks
		mediaRecorder = recorder;
		recordingStartTime = Date.now();

		// Capture is live from this line, so the cue belongs here rather than
		// after the meter setup below, which is another await away.
		playEarcon('ready');

		// Initialize audio monitoring
		const startVisualization = await initAudioMonitoring(mediaStream, myRun);
		if (!isCurrentRun(myRun)) return;

		// Show recording UI
		globalSttState.update((s) => ({
			...s,
			isPreparing: false,
			isRecording: true
		}));

		// Start visualization loop
		startVisualization();
		if (inputMode === 'auto') noSpeechTimer = setTimeout(autoStop, NO_SPEECH_AUTO_STOP_MS);
	} catch (error: unknown) {
		if (!isCurrentRun(myRun)) return;
		clearAutoStop();
		stopAudioMonitoring();
		if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
		mediaStream?.getTracks().forEach((track) => track.stop());
		mediaRecorder = null;
		mediaStream = null;
		settleSink();
		console.error('[GlobalSTT] Failed to start recording:', error);
		const err = error as { name?: string; message?: string };

		let errorMessage: string;
		if (err.name === 'NotAllowedError') {
			errorMessage = 'Microphone access denied. Please allow microphone access in your browser settings.';
		} else if (err.name === 'NotFoundError') {
			errorMessage = 'No microphone found. Please connect a microphone and try again.';
		} else {
			errorMessage = `Failed to start recording: ${err.message || 'Unknown error'}`;
		}

		globalSttState.update((s) => ({
			...s,
			isPreparing: false,
			mode: 'idle',
			error: errorMessage
		}));
	}
}

async function stopRecording(): Promise<void> {
	if (get(globalSttState).mode === 'live') {
		await stopLiveRecording();
		return;
	}

	clearAutoStop();
	playEarcon('stop');

	// Stop audio monitoring first
	stopAudioMonitoring();

	if (!mediaRecorder || mediaRecorder.state === 'inactive') {
		settleSink();
		globalSttState.update((s) => ({ ...s, isRecording: false, mode: 'idle' }));
		return;
	}

	const recorder = mediaRecorder;
	const stream = mediaStream;
	const mimeType = recorder.mimeType;

	// Check minimum recording duration
	const recordingDuration = Date.now() - recordingStartTime;
	if (recordingDuration < MIN_RECORDING_DURATION) {
		settleSink();
		globalSttState.update((s) => ({
			...s,
			isRecording: false,
			mode: 'idle',
			error: 'That was too short to transcribe. Start again and speak for a little longer.'
		}));
		recorder.stop();
		stream?.getTracks().forEach((track) => track.stop());
		mediaRecorder = null;
		mediaStream = null;
		return;
	}

	const myRun = runId;
	globalSttState.update((s) => ({ ...s, isRecording: false, isTranscribing: true }));

	try {
		// Wait for the recorder to flush before assembling the blob.
		//
		// Collection stays on the `ondataavailable` property handler installed in
		// startRecording — it already receives every chunk, including the ones
		// requestData() and stop() emit. A second listener here appended those
		// final chunks TWICE, so the blob ended with duplicated WebM clusters:
		// depending on the decoder that garbled the last words or made
		// decodeAudioData throw, which silently uploaded the malformed original.
		const audioBlob = await new Promise<Blob>((resolve, reject) => {
			const handleStop = () => {
				recorder.removeEventListener('stop', handleStop);
				stream?.getTracks().forEach((track) => track.stop());

				if (audioChunks.length === 0) {
					reject(new Error('No audio recorded'));
					return;
				}

				resolve(new Blob(audioChunks, { type: mimeType }));
			};

			recorder.addEventListener('stop', handleStop);

			// Deterministically flush the tail first: Safari has been unreliable
			// about dispatching the last dataavailable before the stop event.
			recorder.requestData();
			recorder.stop();
		});

		if (!isCurrentRun(myRun)) return;
		mediaRecorder = null;
		mediaStream = null;
		await transcribeAndOutput(audioBlob, myRun);
	} catch (error: unknown) {
		if (!isCurrentRun(myRun)) return;
		settleSink();
		const err = error as { message?: string };
		globalSttState.update((s) => ({
			...s,
			isTranscribing: false,
			mode: 'idle',
			error: err.message || 'Failed to process recording'
		}));
		mediaRecorder = null;
		mediaStream = null;
	}
}

async function toggleRecording(source: GlobalSttState['source'] = 'global'): Promise<void> {
	// Must be the first statement and must stay synchronous: this call runs
	// inside the click/keydown, which is the only moment iOS lets an
	// AudioContext start. By the time a cue is due we are several awaits away.
	primeEarcons();

	// Prevent rapid toggles causing race conditions
	if (isToggling) return;
	isToggling = true;

	try {
		const state = get(globalSttState);

		if (state.isRecording) {
			await stopRecording();
		} else if (!state.isTranscribing && !state.isPreparing) {
			await startRecording(source);
		}
	} finally {
		isToggling = false;
	}
}

// ============================================================================
// Transcription & Output
// ============================================================================

interface TranscribeResponse {
	ok: boolean;
	text?: string;
	detail?: string;
}

/**
 * Upload one recording for transcription.
 *
 * XMLHttpRequest rather than fetch purely for `upload.onprogress`: the upload is
 * the leg that actually hurts on a phone, and it is the only part of the wait we
 * can report honestly. Once the bytes are up, progress goes indeterminate rather
 * than inventing a number for the model's own inference time.
 */
function post(blob: Blob, name: string): Promise<TranscribeResponse> {
	return new Promise((resolve, reject) => {
		const formData = new FormData();
		formData.append('file', blob, name);

		const xhr = new XMLHttpRequest();
		uploadRequest = xhr;
		xhr.open('POST', `${apiBase()}/settings/transcribe`);
		xhr.withCredentials = requestCredentials() === 'include';
		for (const [name, value] of Object.entries(authHeaders())) xhr.setRequestHeader(name, value);

		globalSttUpload.set(0);
		xhr.upload.onprogress = (event) => {
			if (event.lengthComputable) {
				globalSttUpload.set(event.loaded / event.total);
			}
		};
		xhr.upload.onload = () => globalSttUpload.set(1);

		xhr.onload = () => {
			uploadRequest = null;
			let body: { text?: string; detail?: string } = {};
			try {
				body = JSON.parse(xhr.responseText);
			} catch {
				/* a non-JSON body is reported through `detail` below */
			}
			resolve({
				ok: xhr.status >= 200 && xhr.status < 300,
				text: body.text,
				detail: body.detail || (xhr.status ? `Transcription failed (${xhr.status})` : undefined)
			});
		};
		xhr.onerror = () => {
			uploadRequest = null;
			reject(new Error('Network error while uploading the recording.'));
		};
		xhr.onabort = () => {
			uploadRequest = null;
			reject(new Error('Upload cancelled'));
		};

		xhr.send(formData);
	});
}

async function transcribeAndOutput(audioBlob: Blob, myRun: number): Promise<void> {
	globalSttState.update((s) => ({
		...s,
		isTranscribing: true,
		error: null
	}));

	try {
		// MediaRecorder produces WebM/Opus (or MP4). Some STT models (notably
		// ElevenLabs Scribe) reject those, which is why this used to re-encode to
		// 16 kHz WAV every time — but WAV is uncompressed, so a 3-minute dictation
		// went from a few hundred KB of Opus to ~5.8 MB, and that whole inflation
		// crossed a phone's uplink before transcription could even start. Models
		// known to accept the native container now upload it as-is.
		let uploadBlob = audioBlob;
		let filename = `recording.${getFileExtension(audioBlob.type || currentMimeType)}`;
		let converted = false;

		if (!sttAcceptsCompressed) {
			try {
				uploadBlob = await convertToWav(audioBlob);
				filename = 'recording.wav';
				converted = true;
			} catch (conversionError) {
				console.warn('[GlobalSTT] WAV conversion failed, uploading original recording:', conversionError);
			}
		}

		if (!isCurrentRun(myRun)) return;
		let response = await post(uploadBlob, filename);
		if (!isCurrentRun(myRun)) return;

		// Safety net for the allowlist above: if a model is flagged as accepting
		// compressed audio but actually rejects it, convert and retry exactly
		// once. A mis-declared model then costs one slow request rather than
		// breaking voice input outright.
		if (!response.ok && !converted) {
			try {
				const wav = await convertToWav(audioBlob);
				if (!isCurrentRun(myRun)) return;
				console.warn('[GlobalSTT] Native upload rejected, retrying as WAV');
				response = await post(wav, 'recording.wav');
				if (!isCurrentRun(myRun)) return;
			} catch (conversionError) {
				console.warn('[GlobalSTT] WAV retry unavailable:', conversionError);
			}
		}

		if (!response.ok) {
			throw new Error(response.detail || 'Transcription failed');
		}

		const text = (response.text || '').trim();

		if (text) {
			await outputText(text, myRun);
			globalSttState.update((s) => ({
				...s,
				lastTranscription: text
			}));
		} else {
			globalSttState.update((s) => ({
				...s,
				error: 'No speech detected'
			}));
			playEarcon('error');
		}
	} catch (error: unknown) {
		// A cancelled run aborts its own upload, so this fires by design there.
		// Reporting it would stamp an error onto state that has already moved on.
		if (!isCurrentRun(myRun)) return;
		console.error('[GlobalSTT] Transcription failed:', error);
		const err = error as { message?: string };
		globalSttState.update((s) => ({
			...s,
			error: err.message || 'Transcription failed'
		}));
		playEarcon('error');
	} finally {
		if (isCurrentRun(myRun)) {
			settleSink();
			globalSttUpload.set(null);
			globalSttState.update((s) => ({
				...s,
				isTranscribing: false,
				mode: 'idle'
			}));
		}
	}
}

async function outputText(text: string, myRun?: number): Promise<void> {
	if (myRun !== undefined && !isCurrentRun(myRun)) return;

	if (outputSink) {
		outputSink(text);
		return;
	}

	// Always copy to clipboard first
	try {
		await navigator.clipboard.writeText(text);
	} catch (e) {
		console.error('[GlobalSTT] Failed to copy to clipboard:', e);
		// Continue anyway - we'll try to paste if possible
	}
	if (myRun !== undefined && !isCurrentRun(myRun)) return;

	// Check if an input element is focused
	const activeElement = document.activeElement;
	const isInputFocused =
		activeElement instanceof HTMLInputElement ||
		activeElement instanceof HTMLTextAreaElement ||
		(activeElement instanceof HTMLElement && activeElement.isContentEditable);

	if (isInputFocused) {
		// Try to insert text at cursor position
		try {
			// execCommand is deprecated but still widely supported and works well for this
			const inserted = document.execCommand('insertText', false, text);

			if (!inserted) {
				// Fallback: try input event dispatch for some frameworks
				if (activeElement instanceof HTMLInputElement || activeElement instanceof HTMLTextAreaElement) {
					const start = activeElement.selectionStart || 0;
					const end = activeElement.selectionEnd || 0;
					const currentValue = activeElement.value;
					activeElement.value = currentValue.slice(0, start) + text + currentValue.slice(end);
					activeElement.selectionStart = activeElement.selectionEnd = start + text.length;
					activeElement.dispatchEvent(new Event('input', { bubbles: true }));
				}
			}

		} catch (e) {
			console.error('[GlobalSTT] Failed to insert text:', e);
			// Text is already on clipboard, user can paste manually
		}
	}
}

// ============================================================================
// Shortcut Registration
// ============================================================================

function registerGlobalSttShortcut(config: HotkeyConfig): void {
	// Unregister previous if exists
	if (currentRegistration) {
		currentRegistration.unregister();
		currentRegistration = null;
	}

	currentRegistration = registerShortcut({
		id: 'global-stt',
		description: 'Global voice input',
		key: config.key,
		cmdOrCtrl: config.cmdOrCtrl,
		shift: config.shift,
		alt: config.alt,
		category: 'general',
		allowInInput: true, // CRITICAL: Works even in input fields
		preventDefault: true,
		action: toggleRecording
	});
}

function unregisterGlobalSttShortcut(): void {
	if (currentRegistration) {
		currentRegistration.unregister();
		currentRegistration = null;
	}
}

// ============================================================================
// Public API
// ============================================================================

/**
 * Initialize global STT system
 * Call from +page.svelte onMount
 * @returns Cleanup function to call on unmount
 */
export function initGlobalStt(): () => void {
	if (!browser) {
		return () => {};
	}

	// Warm the capability probe so the first press does not pay for it. Failure
	// is expected and ignorable here — this can run before auth has settled.
	void fetchSttCapability().catch(() => {});
	void loadVoiceSettings().catch(() => {});

	// Subscribe to preference changes
	const unsubscribe = hotkeyPreferences.subscribe((prefs) => {
		setEarconsEnabled(prefs.voiceSoundCues !== false);

		if (prefs.globalSttEnabled) {
			registerGlobalSttShortcut(prefs.globalSttHotkey);
		} else {
			unregisterGlobalSttShortcut();
		}
	});

	// Return cleanup function
	return () => {
		unsubscribe();
		unregisterGlobalSttShortcut();
		cleanup();
	};
}

/**
 * Cleanup resources (stop recording if active)
 */
function cleanup(): void {
	stopAudioMonitoring();

	streamSession?.abort();
	streamSession = null;

	if (mediaRecorder && mediaRecorder.state !== 'inactive') {
		mediaRecorder.stop();
	}

	if (mediaStream) {
		mediaStream.getTracks().forEach((track) => track.stop());
	}

	mediaRecorder = null;
	mediaStream = null;
	audioChunks = [];

	settleSink();
	clearAutoStop();
	runId++;
	globalSttState.set(initialState);
	globalSttAudio.set({ level: 0, bands: emptyBands() });
	globalSttUpload.set(null);
}

/**
 * Clear any error message
 */
export function clearError(): void {
	globalSttState.update((s) => ({ ...s, error: null }));
}

/**
 * Abandon voice input in flight. Explicit Discard removes owned speech;
 * navigation may retain the already-visible words with `discard: false`.
 *
 * Bumping the run id is the load-bearing part. Resetting the store would not be
 * enough: `stopRecording` and `stopLiveRecording` hold local references to the
 * recorder and session, and their continuations would still insert the text the
 * user just discarded. Every such continuation checks the id after each await.
 *
 * This is also the only way out of a stuck `isPreparing` — a pending
 * `startSttStream` cannot be aborted from outside, so instead it is left to
 * resolve into a stale run that immediately tears its own session down.
 */
export function cancelVoiceInput({ discard = true }: { discard?: boolean } = {}): void {
	if (!browser) return;

	const wasRecording = get(globalSttState).isRecording;
	runId++;
	clearAutoStop();
	settleSink(discard);

	uploadRequest?.abort();
	uploadRequest = null;

	streamSession?.abort();
	streamSession = null;

	stopAudioMonitoring();

	if (mediaRecorder && mediaRecorder.state !== 'inactive') {
		try {
			mediaRecorder.stop();
		} catch {
			/* already stopping */
		}
	}
	mediaStream?.getTracks().forEach((track) => track.stop());
	mediaRecorder = null;
	mediaStream = null;
	audioChunks = [];

	// Navigation also cleans up completed runs; only active recording needs a stop cue.
	if (wasRecording) playEarcon('stop');
	globalSttUpload.set(null);
	globalSttState.set({ ...initialState });
}

/**
 * Manually trigger recording toggle (for UI buttons)
 */
export function triggerToggle(): void {
	toggleRecording();
}

/**
 * Toggle voice input on behalf of a UI button, delivering the finished text to
 * `sink` instead of the clipboard/caret.
 *
 * Both entry points share one capture stack and automatic end-of-speech stop.
 */
export function toggleVoiceInput(
	sink: (text: string) => void,
	live?: VoiceInputSink
): void {
	const state = get(globalSttState);
	if (!isToggling && !state.isRecording && !state.isPreparing && !state.isTranscribing) {
		outputSink = sink;
		liveSink = live ?? null;
	}
	void toggleRecording('composer');
}
