/**
 * Audio Service - Voice Activity Detection and Audio Management
 *
 * Provides:
 * - Microphone capture with Web Audio API
 * - Voice Activity Detection (VAD) - detects when user starts/stops speaking
 * - Audio level visualization data
 * - TTS audio playback with queue management
 * - Interruption handling (stop TTS when user speaks)
 */

// ============================================================================
// Types
// ============================================================================

export interface AudioServiceOptions {
	/** VAD sensitivity threshold (0.0 - 1.0). Higher = more sensitive */
	vadSensitivity?: number;
	/** How long silence (ms) before considering speech ended */
	silenceThresholdMs?: number;
	/** Minimum speech duration (ms) to be considered valid */
	minSpeechDurationMs?: number;
	/** Audio sample rate for analysis */
	sampleRate?: number;
	/** FFT size for frequency analysis */
	fftSize?: number;
}

export interface VADState {
	isSpeaking: boolean;
	silenceDuration: number;
	speechDuration: number;
	audioLevel: number;  // 0.0 - 1.0, normalized RMS level
}

export type VADCallback = (state: VADState) => void;
export type SpeechEndCallback = (audioBlob: Blob) => void;
export type AudioLevelCallback = (level: number) => void;

// ============================================================================
// Default Configuration
// ============================================================================

const DEFAULT_OPTIONS: Required<AudioServiceOptions> = {
	vadSensitivity: 0.015,     // Default noise floor threshold
	silenceThresholdMs: 1200,  // 1.2s of silence = end of speech
	minSpeechDurationMs: 300,  // Min 300ms to be valid speech
	sampleRate: 16000,         // 16kHz is good for speech
	fftSize: 2048,             // FFT size for analysis
};

// ============================================================================
// Audio Service Class
// ============================================================================

export class AudioService {
	private options: Required<AudioServiceOptions>;
	private audioContext: AudioContext | null = null;
	private mediaStream: MediaStream | null = null;
	private mediaRecorder: MediaRecorder | null = null;
	private analyser: AnalyserNode | null = null;
	private sourceNode: MediaStreamAudioSourceNode | null = null;

	// Audio data
	private audioChunks: Blob[] = [];
	private analyserData: Uint8Array | null = null;

	// VAD state
	private vadState: VADState = {
		isSpeaking: false,
		silenceDuration: 0,
		speechDuration: 0,
		audioLevel: 0,
	};

	// Timing
	private lastVADTime: number = 0;
	private animationFrameId: number | null = null;

	// Callbacks
	private onVADStateChange: VADCallback | null = null;
	private onSpeechEnd: SpeechEndCallback | null = null;
	private onAudioLevel: AudioLevelCallback | null = null;

	// TTS playback
	private ttsAudioQueue: HTMLAudioElement[] = [];
	private currentTtsAudio: HTMLAudioElement | null = null;
	private ttsVolume: number = 1.0;

	// State
	private isCapturing: boolean = false;
	private isMuted: boolean = false;

	constructor(options: AudioServiceOptions = {}) {
		this.options = { ...DEFAULT_OPTIONS, ...options };
	}

	// ========================================================================
	// Lifecycle
	// ========================================================================

	/**
	 * Start audio capture and VAD processing
	 */
	async start(): Promise<void> {
		if (this.isCapturing) {
			console.warn('[AudioService] Already capturing');
			return;
		}

		try {
			// Get microphone access
			this.mediaStream = await navigator.mediaDevices.getUserMedia({
				audio: {
					echoCancellation: true,
					noiseSuppression: true,
					autoGainControl: true,
				},
			});

			// Create audio context for analysis
			this.audioContext = new AudioContext();

			// Create analyser for VAD
			this.analyser = this.audioContext.createAnalyser();
			this.analyser.fftSize = this.options.fftSize;
			this.analyser.smoothingTimeConstant = 0.8;
			this.analyserData = new Uint8Array(this.analyser.frequencyBinCount);

			// Connect microphone to analyser
			this.sourceNode = this.audioContext.createMediaStreamSource(this.mediaStream);
			this.sourceNode.connect(this.analyser);

			// Set up MediaRecorder for capturing audio
			const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
				? 'audio/webm;codecs=opus'
				: MediaRecorder.isTypeSupported('audio/webm')
					? 'audio/webm'
					: 'audio/mp4';

			this.mediaRecorder = new MediaRecorder(this.mediaStream, { mimeType });
			this.audioChunks = [];

			this.mediaRecorder.ondataavailable = (event) => {
				if (event.data.size > 0) {
					this.audioChunks.push(event.data);
				}
			};

			// Start recording in chunks for reliable data capture
			this.mediaRecorder.start(500); // 500ms chunks

			// Reset VAD state
			this.vadState = {
				isSpeaking: false,
				silenceDuration: 0,
				speechDuration: 0,
				audioLevel: 0,
			};
			this.lastVADTime = performance.now();

			// Start VAD processing loop
			this.isCapturing = true;
			this.processVAD();

			console.log('[AudioService] Started audio capture');
		} catch (error) {
			console.error('[AudioService] Failed to start:', error);
			throw error;
		}
	}

	/**
	 * Stop audio capture and cleanup
	 */
	stop(): void {
		if (!this.isCapturing) return;

		this.isCapturing = false;

		// Stop animation frame
		if (this.animationFrameId !== null) {
			cancelAnimationFrame(this.animationFrameId);
			this.animationFrameId = null;
		}

		// Stop MediaRecorder
		if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
			this.mediaRecorder.stop();
		}
		this.mediaRecorder = null;

		// Stop media stream tracks
		if (this.mediaStream) {
			this.mediaStream.getTracks().forEach((track) => track.stop());
			this.mediaStream = null;
		}

		// Disconnect audio nodes
		if (this.sourceNode) {
			this.sourceNode.disconnect();
			this.sourceNode = null;
		}

		// Close audio context
		if (this.audioContext) {
			this.audioContext.close();
			this.audioContext = null;
		}

		this.analyser = null;
		this.analyserData = null;
		this.audioChunks = [];

		console.log('[AudioService] Stopped audio capture');
	}

	/**
	 * Check if currently capturing
	 */
	get capturing(): boolean {
		return this.isCapturing;
	}

	// ========================================================================
	// VAD Processing
	// ========================================================================

	/**
	 * Main VAD processing loop (runs on animation frame)
	 */
	private processVAD = (): void => {
		if (!this.isCapturing || !this.analyser || !this.analyserData) {
			return;
		}

		// Get frequency data
		this.analyser.getByteFrequencyData(this.analyserData);

		// Calculate RMS level from frequency data
		let sum = 0;
		for (let i = 0; i < this.analyserData.length; i++) {
			const normalized = this.analyserData[i] / 255;
			sum += normalized * normalized;
		}
		const rms = Math.sqrt(sum / this.analyserData.length);

		// Update audio level (smoothed)
		this.vadState.audioLevel = this.vadState.audioLevel * 0.7 + rms * 0.3;

		// Calculate time delta
		const now = performance.now();
		const deltaMs = now - this.lastVADTime;
		this.lastVADTime = now;

		// Determine if voice is detected based on sensitivity threshold
		// Adjust threshold based on sensitivity setting (0-1)
		// Higher sensitivity = lower threshold
		const threshold = this.options.vadSensitivity * (1.1 - this.options.vadSensitivity);
		const isVoiceDetected = this.vadState.audioLevel > threshold;

		// Update VAD state machine
		const wasSpeaking = this.vadState.isSpeaking;

		if (isVoiceDetected) {
			// Voice detected
			if (!this.isMuted) {
				this.vadState.isSpeaking = true;
				this.vadState.speechDuration += deltaMs;
				this.vadState.silenceDuration = 0;

				// If we just started speaking, handle interruption
				if (!wasSpeaking) {
					this.handleSpeechStart();
				}
			}
		} else {
			// Silence detected
			if (this.vadState.isSpeaking) {
				this.vadState.silenceDuration += deltaMs;

				// Check if silence threshold exceeded
				if (this.vadState.silenceDuration >= this.options.silenceThresholdMs) {
					// End of speech detected
					if (this.vadState.speechDuration >= this.options.minSpeechDurationMs) {
						this.handleSpeechEnd();
					} else {
						// Too short, ignore
						this.resetVADState();
					}
				}
			}
		}

		// Notify audio level callback
		if (this.onAudioLevel) {
			this.onAudioLevel(this.vadState.audioLevel);
		}

		// Notify VAD state change callback
		if (this.onVADStateChange) {
			this.onVADStateChange({ ...this.vadState });
		}

		// Continue loop
		this.animationFrameId = requestAnimationFrame(this.processVAD);
	};

	/**
	 * Handle speech start (potential interruption)
	 */
	private handleSpeechStart(): void {
		console.log('[AudioService] Speech started');

		// Stop any playing TTS (interruption)
		this.stopTts();

		// Clear and restart recording to capture this utterance fresh
		this.audioChunks = [];
		if (this.mediaRecorder && this.mediaRecorder.state === 'recording') {
			// Request any buffered data, then we'll continue capturing
			this.mediaRecorder.requestData();
		}
	}

	/**
	 * Handle speech end - collect audio and notify callback
	 */
	private handleSpeechEnd(): void {
		console.log('[AudioService] Speech ended, duration:', this.vadState.speechDuration, 'ms');

		// Stop recording to flush remaining data
		if (this.mediaRecorder && this.mediaRecorder.state === 'recording') {
			this.mediaRecorder.requestData();

			// Small delay to ensure ondataavailable fires
			setTimeout(() => {
				if (this.audioChunks.length > 0) {
					const mimeType = this.mediaRecorder?.mimeType || 'audio/webm';
					const audioBlob = new Blob(this.audioChunks, { type: mimeType });

					if (this.onSpeechEnd) {
						this.onSpeechEnd(audioBlob);
					}
				}

				// Reset state
				this.resetVADState();

				// Clear chunks for next utterance
				this.audioChunks = [];
			}, 100);
		} else {
			this.resetVADState();
		}
	}

	/**
	 * Reset VAD state
	 */
	private resetVADState(): void {
		this.vadState.isSpeaking = false;
		this.vadState.speechDuration = 0;
		this.vadState.silenceDuration = 0;
	}

	// ========================================================================
	// Callbacks
	// ========================================================================

	/**
	 * Set callback for VAD state changes
	 */
	onVadStateChange(callback: VADCallback | null): void {
		this.onVADStateChange = callback;
	}

	/**
	 * Set callback for when speech ends (returns audio blob)
	 */
	onSpeechEnded(callback: SpeechEndCallback | null): void {
		this.onSpeechEnd = callback;
	}

	/**
	 * Set callback for audio level updates
	 */
	onAudioLevelChange(callback: AudioLevelCallback | null): void {
		this.onAudioLevel = callback;
	}

	// ========================================================================
	// Mute Control
	// ========================================================================

	/**
	 * Set mute state
	 */
	setMuted(muted: boolean): void {
		this.isMuted = muted;

		// If we're muting while speaking, end the utterance
		if (muted && this.vadState.isSpeaking) {
			this.resetVADState();
		}
	}

	/**
	 * Get current mute state
	 */
	get muted(): boolean {
		return this.isMuted;
	}

	// ========================================================================
	// TTS Playback
	// ========================================================================

	/**
	 * Queue and play TTS audio
	 * @param audioUrl URL of the audio to play
	 * @returns Promise that resolves when audio finishes playing
	 */
	async playTts(audioUrl: string): Promise<void> {
		return new Promise((resolve, reject) => {
			const audio = new Audio(audioUrl);
			audio.volume = this.ttsVolume;

			audio.onended = () => {
				this.currentTtsAudio = null;
				this.playNextInQueue();
				resolve();
			};

			audio.onerror = (e) => {
				console.error('[AudioService] TTS playback error:', e);
				this.currentTtsAudio = null;
				this.playNextInQueue();
				reject(e);
			};

			// If something is playing, queue it
			if (this.currentTtsAudio) {
				this.ttsAudioQueue.push(audio);
			} else {
				this.currentTtsAudio = audio;
				audio.play().catch(reject);
			}
		});
	}

	/**
	 * Play next audio in queue
	 */
	private playNextInQueue(): void {
		if (this.ttsAudioQueue.length > 0) {
			this.currentTtsAudio = this.ttsAudioQueue.shift()!;
			this.currentTtsAudio.play().catch((e) => {
				console.error('[AudioService] Queue playback error:', e);
				this.playNextInQueue();
			});
		}
	}

	/**
	 * Stop TTS playback and clear queue
	 */
	stopTts(): void {
		if (this.currentTtsAudio) {
			this.currentTtsAudio.pause();
			this.currentTtsAudio.currentTime = 0;
			this.currentTtsAudio = null;
		}

		// Clear queue
		this.ttsAudioQueue.forEach((audio) => {
			audio.pause();
		});
		this.ttsAudioQueue = [];

		console.log('[AudioService] TTS stopped (interrupted)');
	}

	/**
	 * Set TTS volume
	 */
	setTtsVolume(volume: number): void {
		this.ttsVolume = Math.max(0, Math.min(1, volume));

		if (this.currentTtsAudio) {
			this.currentTtsAudio.volume = this.ttsVolume;
		}
	}

	/**
	 * Check if TTS is currently playing
	 */
	get isTtsPlaying(): boolean {
		return this.currentTtsAudio !== null && !this.currentTtsAudio.paused;
	}

	// ========================================================================
	// Settings
	// ========================================================================

	/**
	 * Update VAD sensitivity
	 */
	setVadSensitivity(sensitivity: number): void {
		this.options.vadSensitivity = Math.max(0, Math.min(1, sensitivity));
	}

	/**
	 * Update silence threshold
	 */
	setSilenceThreshold(ms: number): void {
		this.options.silenceThresholdMs = Math.max(500, Math.min(5000, ms));
	}
}

// ============================================================================
// Singleton Export
// ============================================================================

// Create a singleton instance for global use
let audioServiceInstance: AudioService | null = null;

export function getAudioService(options?: AudioServiceOptions): AudioService {
	if (!audioServiceInstance) {
		audioServiceInstance = new AudioService(options);
	}
	return audioServiceInstance;
}

export function resetAudioService(): void {
	if (audioServiceInstance) {
		audioServiceInstance.stop();
		audioServiceInstance = null;
	}
}
