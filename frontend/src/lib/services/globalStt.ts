/**
 * Global STT Service
 *
 * Provides global Speech-to-Text functionality triggered by a configurable hotkey.
 * When triggered:
 * 1. Records audio from the microphone
 * 2. Transcribes via the backend API
 * 3. Copies result to clipboard
 * 4. If an input field is focused, also pastes the text
 */

import { writable, get } from 'svelte/store';
import { browser } from '$app/environment';
import { registerShortcut, type ShortcutRegistration } from './keyboard';
import { hotkeyPreferences, type HotkeyConfig } from '$lib/stores/hotkeyPreferences';

// ============================================================================
// Types
// ============================================================================

export interface GlobalSttState {
	isRecording: boolean;
	isTranscribing: boolean;
	isPreparing: boolean;
	audioLevel: number;
	error: string | null;
	lastTranscription: string | null;
}

// ============================================================================
// State Store
// ============================================================================

const initialState: GlobalSttState = {
	isRecording: false,
	isTranscribing: false,
	isPreparing: false,
	audioLevel: 0,
	error: null,
	lastTranscription: null
};

export const globalSttState = writable<GlobalSttState>(initialState);

// ============================================================================
// Private State
// ============================================================================

let currentRegistration: ShortcutRegistration | null = null;
let mediaRecorder: MediaRecorder | null = null;
let mediaStream: MediaStream | null = null;
let audioChunks: Blob[] = [];
let recordingStartTime: number = 0;

// Audio level monitoring
let audioContext: AudioContext | null = null;
let analyserNode: AnalyserNode | null = null;
let audioLevelAnimationId: number | null = null;

const MIN_RECORDING_DURATION = 500; // 500ms minimum

// Mutex to prevent race conditions during toggle
let isToggling = false;

// Track current MIME type for proper file extension
let currentMimeType: string = 'audio/webm';

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

async function initAudioMonitoring(stream: MediaStream): Promise<() => void> {
	audioContext = new AudioContext();
	analyserNode = audioContext.createAnalyser();
	analyserNode.fftSize = 256;
	analyserNode.smoothingTimeConstant = 0.8;

	const source = audioContext.createMediaStreamSource(stream);
	source.connect(analyserNode);

	const dataArray = new Uint8Array(analyserNode.frequencyBinCount);

	function updateLevel() {
		if (!analyserNode) return;

		analyserNode.getByteFrequencyData(dataArray);

		// Calculate RMS level
		let sum = 0;
		for (let i = 0; i < dataArray.length; i++) {
			const normalized = dataArray[i] / 255;
			sum += normalized * normalized;
		}
		const rms = Math.sqrt(sum / dataArray.length);

		// Update state with smoothed level
		globalSttState.update((s) => ({
			...s,
			audioLevel: s.audioLevel * 0.7 + rms * 0.3
		}));

		audioLevelAnimationId = requestAnimationFrame(updateLevel);
	}

	// Return function to start the loop
	return () => {
		updateLevel();
	};
}

function stopAudioMonitoring(): void {
	if (audioLevelAnimationId !== null) {
		cancelAnimationFrame(audioLevelAnimationId);
		audioLevelAnimationId = null;
	}

	if (audioContext) {
		audioContext.close().catch(() => {});
		audioContext = null;
	}

	analyserNode = null;

	globalSttState.update((s) => ({ ...s, audioLevel: 0 }));
}

// ============================================================================
// Recording Functions
// ============================================================================

async function startRecording(): Promise<void> {
	const state = get(globalSttState);

	// Don't start if already recording or transcribing
	if (state.isRecording || state.isTranscribing || state.isPreparing) {
		return;
	}

	audioChunks = [];

	globalSttState.update((s) => ({
		...s,
		isPreparing: true,
		error: null
	}));

	try {
		// Get microphone access
		mediaStream = await navigator.mediaDevices.getUserMedia({
			audio: {
				echoCancellation: true,
				noiseSuppression: true,
				autoGainControl: true
			}
		});

		// Determine best supported mime type
		currentMimeType = getSupportedMimeType();

		const recorder = new MediaRecorder(mediaStream, { mimeType: currentMimeType });

		// Collect audio chunks
		recorder.ondataavailable = (event) => {
			if (event.data.size > 0) {
				audioChunks.push(event.data);
			}
		};

		// Start recording with timeslice for chunked data collection
		recorder.start(500); // 500ms chunks
		mediaRecorder = recorder;
		recordingStartTime = Date.now();

		// Initialize audio monitoring
		const startVisualization = await initAudioMonitoring(mediaStream);

		// Show recording UI
		globalSttState.update((s) => ({
			...s,
			isPreparing: false,
			isRecording: true
		}));

		// Start visualization loop
		startVisualization();

		console.log('[GlobalSTT] Recording started');
	} catch (error: unknown) {
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
			error: errorMessage
		}));
	}
}

async function stopRecording(): Promise<void> {
	// Stop audio monitoring first
	stopAudioMonitoring();

	if (!mediaRecorder || mediaRecorder.state === 'inactive') {
		globalSttState.update((s) => ({ ...s, isRecording: false }));
		return;
	}

	const recorder = mediaRecorder;
	const stream = mediaStream;
	const mimeType = recorder.mimeType;

	// Check minimum recording duration
	const recordingDuration = Date.now() - recordingStartTime;
	if (recordingDuration < MIN_RECORDING_DURATION) {
		globalSttState.update((s) => ({
			...s,
			isRecording: false,
			error: 'Recording too short. Please hold the hotkey longer.'
		}));
		recorder.stop();
		stream?.getTracks().forEach((track) => track.stop());
		mediaRecorder = null;
		mediaStream = null;
		return;
	}

	globalSttState.update((s) => ({ ...s, isRecording: false }));

	console.log('[GlobalSTT] Recording stopped, processing...');

	try {
		// Use Promise-based approach to ensure all data is collected
		const audioBlob = await new Promise<Blob>((resolve, reject) => {
			const handleDataAvailable = (event: BlobEvent) => {
				if (event.data.size > 0) {
					audioChunks.push(event.data);
				}
			};

			const handleStop = () => {
				recorder.removeEventListener('dataavailable', handleDataAvailable);
				recorder.removeEventListener('stop', handleStop);
				stream?.getTracks().forEach((track) => track.stop());

				if (audioChunks.length === 0) {
					reject(new Error('No audio recorded'));
					return;
				}

				resolve(new Blob(audioChunks, { type: mimeType }));
			};

			recorder.addEventListener('dataavailable', handleDataAvailable);
			recorder.addEventListener('stop', handleStop);

			recorder.requestData();
			recorder.stop();
		});

		mediaRecorder = null;
		mediaStream = null;

		await transcribeAndOutput(audioBlob);
	} catch (error: unknown) {
		const err = error as { message?: string };
		globalSttState.update((s) => ({
			...s,
			error: err.message || 'Failed to process recording'
		}));
		mediaRecorder = null;
		mediaStream = null;
	}
}

async function toggleRecording(): Promise<void> {
	// Prevent rapid toggles causing race conditions
	if (isToggling) return;
	isToggling = true;

	try {
		const state = get(globalSttState);

		if (state.isRecording) {
			await stopRecording();
		} else if (!state.isTranscribing && !state.isPreparing) {
			await startRecording();
		}
	} finally {
		isToggling = false;
	}
}

// ============================================================================
// Transcription & Output
// ============================================================================

async function transcribeAndOutput(audioBlob: Blob): Promise<void> {
	globalSttState.update((s) => ({
		...s,
		isTranscribing: true,
		error: null
	}));

	try {
		const formData = new FormData();
		const extension = getFileExtension(audioBlob.type || currentMimeType);
		formData.append('file', audioBlob, `recording.${extension}`);

		const response = await fetch('/api/v1/settings/transcribe', {
			method: 'POST',
			credentials: 'include',
			body: formData
		});

		if (!response.ok) {
			const error = await response.json().catch(() => ({ detail: 'Transcription failed' }));
			throw new Error(error.detail || 'Transcription failed');
		}

		const result = await response.json();
		const text = result.text?.trim();

		if (text) {
			await outputText(text);
			globalSttState.update((s) => ({
				...s,
				lastTranscription: text
			}));
			console.log('[GlobalSTT] Transcription complete:', text.substring(0, 50) + '...');
		} else {
			globalSttState.update((s) => ({
				...s,
				error: 'No speech detected'
			}));
		}
	} catch (error: unknown) {
		console.error('[GlobalSTT] Transcription failed:', error);
		const err = error as { message?: string };
		globalSttState.update((s) => ({
			...s,
			error: err.message || 'Transcription failed'
		}));
	} finally {
		globalSttState.update((s) => ({
			...s,
			isTranscribing: false
		}));
	}
}

async function outputText(text: string): Promise<void> {
	// Always copy to clipboard first
	try {
		await navigator.clipboard.writeText(text);
		console.log('[GlobalSTT] Text copied to clipboard');
	} catch (e) {
		console.error('[GlobalSTT] Failed to copy to clipboard:', e);
		// Continue anyway - we'll try to paste if possible
	}

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

			console.log('[GlobalSTT] Text inserted into focused input');
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

	console.log('[GlobalSTT] Shortcut registered:', config);
}

function unregisterGlobalSttShortcut(): void {
	if (currentRegistration) {
		currentRegistration.unregister();
		currentRegistration = null;
		console.log('[GlobalSTT] Shortcut unregistered');
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

	// Subscribe to preference changes
	const unsubscribe = hotkeyPreferences.subscribe((prefs) => {
		if (prefs.globalSttEnabled) {
			registerGlobalSttShortcut(prefs.globalSttHotkey);
		} else {
			unregisterGlobalSttShortcut();
		}
	});

	console.log('[GlobalSTT] Initialized');

	// Return cleanup function
	return () => {
		unsubscribe();
		unregisterGlobalSttShortcut();
		cleanup();
		console.log('[GlobalSTT] Cleaned up');
	};
}

/**
 * Cleanup resources (stop recording if active)
 */
function cleanup(): void {
	stopAudioMonitoring();

	if (mediaRecorder && mediaRecorder.state !== 'inactive') {
		mediaRecorder.stop();
	}

	if (mediaStream) {
		mediaStream.getTracks().forEach((track) => track.stop());
	}

	mediaRecorder = null;
	mediaStream = null;
	audioChunks = [];

	globalSttState.set(initialState);
}

/**
 * Clear any error message
 */
export function clearError(): void {
	globalSttState.update((s) => ({ ...s, error: null }));
}

/**
 * Manually trigger recording toggle (for UI buttons)
 */
export function triggerToggle(): void {
	toggleRecording();
}
