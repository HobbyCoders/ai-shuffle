<script lang="ts">
	/**
	 * VoiceRecorder - Handles microphone access, MediaRecorder, audio level
	 * visualization, and transcription. Extracted from ChatInput.
	 */
	import { tick } from 'svelte';

	interface Props {
		onTranscription: (text: string) => void;
		disabled?: boolean;
		isRecording?: boolean;
		isTranscribing?: boolean;
	}

	let {
		onTranscription,
		disabled = false,
		isRecording = $bindable(false),
		isTranscribing = $bindable(false)
	}: Props = $props();

	let isPreparing = $state(false);
	let mediaRecorder = $state<MediaRecorder | null>(null);
	let recordingError = $state('');
	let recordingStartTime = $state<number>(0);
	let audioLevel = $state(0);
	let analyserNode = $state<AnalyserNode | null>(null);
	let audioContext = $state<AudioContext | null>(null);
	let animationFrameId = $state<number | null>(null);

	// Use a non-reactive array for audio chunks to avoid Svelte 5 reactivity issues
	// with rapid MediaRecorder ondataavailable events
	let audioChunksRef: Blob[] = [];

	// Recording is always available for admins (they control the API keys)
	const canRecord = $derived(true);

	// Minimum recording duration in milliseconds to avoid empty/too-short recordings
	const MIN_RECORDING_DURATION = 500;

	// Audio level monitoring for visualization
	// Returns a promise that resolves when the audio system is ready to capture
	// The returned function starts the visualization loop (call after isRecording = true)
	async function initAudioMonitoring(stream: MediaStream): Promise<(() => void) | null> {
		try {
			audioContext = new AudioContext();

			// If AudioContext is suspended (common on some browsers), resume it
			if (audioContext.state === 'suspended') {
				await audioContext.resume();
			}

			const source = audioContext.createMediaStreamSource(stream);
			analyserNode = audioContext.createAnalyser();
			analyserNode.fftSize = 256;
			analyserNode.smoothingTimeConstant = 0.5;
			source.connect(analyserNode);

			const dataArray = new Uint8Array(analyserNode.frequencyBinCount);

			// Wait for AudioContext to be in 'running' state
			// This is the proper API signal that the audio system is ready
			if (audioContext.state !== 'running') {
				const ctx = audioContext; // Capture reference for closure
				await new Promise<void>((resolve) => {
					const handleStateChange = () => {
						if (ctx.state === 'running') {
							ctx.removeEventListener('statechange', handleStateChange);
							resolve();
						}
					};
					ctx.addEventListener('statechange', handleStateChange);

					// Safety timeout - resolve after 1s even if state never changes
					setTimeout(() => {
						ctx.removeEventListener('statechange', handleStateChange);
						resolve();
					}, 1000);
				});
			}

			// Return a function to start the visualization loop
			// This should be called AFTER isRecording is set to true
			return function startVisualization() {
				function updateLevel() {
					if (!analyserNode || !isRecording) {
						audioLevel = 0;
						return;
					}
					analyserNode.getByteFrequencyData(dataArray);
					// Calculate average level
					const average = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;
					// Normalize to 0-1 range with some amplification for better visual feedback
					audioLevel = Math.min(1, average / 128);
					animationFrameId = requestAnimationFrame(updateLevel);
				}
				updateLevel();
			};
		} catch (e) {
			console.warn('Audio level monitoring not available:', e);
			return null;
		}
	}

	function stopAudioLevelMonitoring() {
		if (animationFrameId) {
			cancelAnimationFrame(animationFrameId);
			animationFrameId = null;
		}
		if (audioContext) {
			audioContext.close().catch(() => {});
			audioContext = null;
		}
		analyserNode = null;
		audioLevel = 0;
	}

	async function startRecording() {
		recordingError = '';
		audioChunksRef = [];
		isPreparing = true;

		try {
			if (!navigator.mediaDevices) {
				throw new Error(
					'Microphone requires a secure context (HTTPS or localhost). In Chrome, you can allow this origin at chrome://flags/#unsafely-treat-insecure-origin-as-secure'
				);
			}
			const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

			// Determine best supported mime type
			const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
				? 'audio/webm;codecs=opus'
				: MediaRecorder.isTypeSupported('audio/webm')
					? 'audio/webm'
					: 'audio/mp4';

			const recorder = new MediaRecorder(stream, { mimeType });

			// Collect audio chunks - use non-reactive array to avoid issues
			recorder.ondataavailable = (event) => {
				if (event.data.size > 0) {
					audioChunksRef.push(event.data);
				}
			};

			// Start recording with timeslice for chunked data collection
			recorder.start(1000);
			mediaRecorder = recorder;
			recordingStartTime = Date.now();

			// Initialize audio monitoring and wait for the audio system to be ready
			const startVisualization = await initAudioMonitoring(stream);

			// Only now show the recording UI - system is ready to capture speech
			isPreparing = false;
			isRecording = true;

			// Start the visualization loop now that isRecording is true
			if (startVisualization) {
				startVisualization();
			}
		} catch (error: unknown) {
			isPreparing = false;
			console.error('Failed to start recording:', error);
			const err = error as { name?: string; message?: string };
			if (err.name === 'NotAllowedError') {
				recordingError = 'Microphone access denied. Please allow microphone access in your browser settings.';
			} else if (err.name === 'NotFoundError') {
				recordingError = 'No microphone found. Please connect a microphone and try again.';
			} else {
				recordingError = `Failed to start recording: ${err.message || 'Unknown error'}`;
			}
		}
	}

	async function stopRecording() {
		// Stop audio level monitoring first
		stopAudioLevelMonitoring();

		if (!mediaRecorder || mediaRecorder.state === 'inactive') {
			isRecording = false;
			return;
		}

		const recorder = mediaRecorder;
		const stream = recorder.stream;
		const mimeType = recorder.mimeType;

		// Check minimum recording duration
		const recordingDuration = Date.now() - recordingStartTime;
		if (recordingDuration < MIN_RECORDING_DURATION) {
			recordingError = 'Recording too short. Please hold the button longer.';
			recorder.stop();
			stream.getTracks().forEach(track => track.stop());
			isRecording = false;
			return;
		}

		isRecording = false;

		// Use a Promise-based approach to ensure all data is collected before processing
		try {
			const audioBlob = await new Promise<Blob>((resolve, reject) => {
				const handleDataAvailable = (event: BlobEvent) => {
					if (event.data.size > 0) {
						audioChunksRef.push(event.data);
					}
				};

				const handleStop = () => {
					recorder.removeEventListener('dataavailable', handleDataAvailable);
					recorder.removeEventListener('stop', handleStop);
					stream.getTracks().forEach(track => track.stop());

					if (audioChunksRef.length === 0) {
						reject(new Error('No audio recorded'));
						return;
					}

					resolve(new Blob(audioChunksRef, { type: mimeType }));
				};

				recorder.addEventListener('dataavailable', handleDataAvailable);
				recorder.addEventListener('stop', handleStop);

				recorder.requestData();
				recorder.stop();
			});

			await transcribeAudio(audioBlob);
		} catch (error: unknown) {
			const err = error as { message?: string };
			recordingError = err.message || 'Failed to process recording';
		}
	}

	async function transcribeAudio(audioBlob: Blob) {
		isTranscribing = true;
		recordingError = '';

		try {
			const formData = new FormData();
			formData.append('file', audioBlob, 'recording.webm');

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
			const transcribedText = result.text?.trim();

			if (transcribedText) {
				onTranscription(transcribedText);
			}
		} catch (error: unknown) {
			console.error('Transcription failed:', error);
			const err = error as { message?: string };
			recordingError = err.message || 'Failed to transcribe audio';
		} finally {
			isTranscribing = false;
		}
	}

	async function toggleRecording() {
		if (isRecording) {
			await stopRecording();
		} else {
			await startRecording();
		}
	}
</script>

<!-- Recording Error Message -->
{#if recordingError}
	<div class="error-banner">
		<span>{recordingError}</span>
		<button type="button" onclick={() => recordingError = ''} class="error-close">&times;</button>
	</div>
{/if}

<!-- Voice button is rendered inline within the send-button-wrapper -->
<div class="voice-btn-wrapper">
	<button
		type="button"
		onclick={toggleRecording}
		disabled={disabled || isTranscribing || isPreparing || !canRecord}
		class="voice-btn"
		class:preparing={isPreparing}
		class:recording={isRecording}
		class:transcribing={isTranscribing}
		title={!canRecord ? 'Configure OpenAI API key in Settings to use voice input' : isPreparing ? 'Preparing...' : isRecording ? 'Tap to stop' : isTranscribing ? 'Transcribing...' : 'Voice input'}
		style="--audio-level: {audioLevel}"
	>
		{#if isRecording}
			<div class="voice-rings">
				<div class="voice-ring voice-ring-1" style="transform: scale({1 + audioLevel * 0.3}); opacity: {0.3 + audioLevel * 0.4}"></div>
				<div class="voice-ring voice-ring-2" style="transform: scale({1 + audioLevel * 0.5}); opacity: {0.2 + audioLevel * 0.3}"></div>
				<div class="voice-ring voice-ring-3" style="transform: scale({1 + audioLevel * 0.7}); opacity: {0.1 + audioLevel * 0.2}"></div>
			</div>
		{/if}
		<div class="voice-btn-content">
			{#if isTranscribing}
				<div class="voice-processing">
					<span class="processing-dot"></span>
					<span class="processing-dot"></span>
					<span class="processing-dot"></span>
				</div>
			{:else if isPreparing}
				<svg class="voice-icon voice-icon-preparing" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" />
				</svg>
			{:else if isRecording}
				<div class="voice-waveform">
					<span class="waveform-bar" style="height: {20 + audioLevel * 60}%"></span>
					<span class="waveform-bar" style="height: {40 + audioLevel * 50}%"></span>
					<span class="waveform-bar" style="height: {30 + audioLevel * 70}%"></span>
					<span class="waveform-bar" style="height: {50 + audioLevel * 40}%"></span>
					<span class="waveform-bar" style="height: {25 + audioLevel * 55}%"></span>
				</div>
			{:else}
				<svg class="voice-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" />
				</svg>
			{/if}
		</div>
	</button>
</div>

<style>
	/* Error banner (for recording errors shown above the input) */
	.error-banner {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		margin-bottom: 0.5rem;
		padding: 0.5rem 0.75rem;
		background: color-mix(in srgb, var(--destructive) 10%, transparent);
		border: 1px solid color-mix(in srgb, var(--destructive) 30%, transparent);
		border-radius: var(--radius, 0.75rem);
		font-size: 0.8125rem;
		color: var(--destructive);
	}

	.error-close {
		font-size: 1.25rem;
		line-height: 1;
		opacity: 0.7;
		transition: opacity 0.15s;
	}

	.error-close:hover {
		opacity: 1;
	}

	/* ===== VOICE BUTTON WITH RICH VISUAL FEEDBACK ===== */
	/* Wrapper clips animation rings without affecting sibling dropdown menus */
	.voice-btn-wrapper {
		position: relative;
		/* Clip voice animation rings within this wrapper only */
		overflow: hidden;
		/* Match voice button size plus a bit of padding for the glow */
		/* Use theme radius with max 50% for circular, 0 for square themes */
		border-radius: min(50%, calc(var(--radius, 0.75rem) * 2.67));
		/* Ensure the wrapper doesn't add extra size */
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.voice-btn {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		background: transparent;
		border: none;
		/* Use theme radius with max 50% for circular, 0 for square themes */
		border-radius: min(50%, calc(var(--radius, 0.75rem) * 2.67));
		cursor: pointer;
		color: var(--muted-foreground);
		transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
		overflow: visible;
	}

	.voice-btn:hover:not(:disabled) {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.voice-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	/* Preparing state: pulsing animation */
	.voice-btn.preparing {
		background: var(--status-warning-bg);
		color: var(--warning);
	}

	.voice-btn.preparing .voice-icon-preparing {
		animation: voice-pulse 1s ease-in-out infinite;
	}

	@keyframes voice-pulse {
		0%, 100% {
			transform: scale(1);
			opacity: 0.7;
		}
		50% {
			transform: scale(1.1);
			opacity: 1;
		}
	}

	/* Recording state: vibrant red glow */
	.voice-btn.recording {
		background: var(--status-error-bg);
		color: var(--destructive);
		box-shadow: 0 0 0 2px var(--status-error-border);
	}

	/* Transcribing state: teal processing */
	.voice-btn.transcribing {
		background: var(--chip-primary-bg);
		color: var(--primary);
	}

	/* Voice button content container */
	.voice-btn-content {
		position: relative;
		z-index: 2;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 100%;
		height: 100%;
	}

	/* Mic icon */
	.voice-icon {
		width: 15px;
		height: 15px;
		transition: transform 0.2s ease;
	}

	/* Audio level rings (expand outward when speaking) */
	.voice-rings {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		pointer-events: none;
		z-index: 1;
		overflow: visible;
	}

	.voice-ring {
		position: absolute;
		/* Smaller base size so expanded rings stay more contained */
		width: 80%;
		height: 80%;
		/* Use theme radius with max 50% for circular, 0 for square themes */
		border-radius: min(50%, calc(var(--radius, 0.75rem) * 2.67));
		border: 2px solid var(--status-error-border);
		transition: transform 0.1s ease-out, opacity 0.1s ease-out;
	}

	.voice-ring-1 { animation-delay: 0s; }
	.voice-ring-2 { animation-delay: 0.1s; }
	.voice-ring-3 { animation-delay: 0.2s; }

	/* Waveform visualization */
	.voice-waveform {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 2px;
		height: 15px;
	}

	.waveform-bar {
		width: 3px;
		min-height: 4px;
		background: currentColor;
		border-radius: 1.5px;
		transition: height 0.08s ease-out;
	}

	/* Processing dots animation */
	.voice-processing {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 4px;
	}

	.processing-dot {
		width: 4px;
		height: 4px;
		background: currentColor;
		border-radius: 50%;
		animation: processing-bounce 1.2s infinite ease-in-out;
	}

	.processing-dot:nth-child(1) { animation-delay: 0s; }
	.processing-dot:nth-child(2) { animation-delay: 0.2s; }
	.processing-dot:nth-child(3) { animation-delay: 0.4s; }

	@keyframes processing-bounce {
		0%, 80%, 100% {
			transform: scale(0.8);
			opacity: 0.5;
		}
		40% {
			transform: scale(1.2);
			opacity: 1;
		}
	}

	/* Active/pressed state */
	.voice-btn:active:not(:disabled) {
		transform: scale(0.95);
	}
</style>
