<script lang="ts">
	/**
	 * VoiceChatView - Full voice session experience
	 *
	 * Two display modes:
	 * - Orb Mode (default): Immersive audio-reactive visualization
	 * - Transcript Mode: Text-based message view with audio controls overlay
	 *
	 * Reads from the activeSession store like ChatView.
	 */
	import { onDestroy } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import MessageArea from '$lib/components/chat/MessageArea.svelte';
	import RealtimeVoicePanel from '$lib/components/voice/RealtimeVoicePanel.svelte';
	import {
		Mic, MicOff, PhoneOff, Settings, MessageSquareText,
		AudioWaveform, ChevronLeft, Zap
	} from 'lucide-svelte';

	interface Props {
		onSpeechEnd?: (audioBlob: Blob) => void;
		onInterrupt?: () => void;
	}

	let {
		onSpeechEnd,
		onInterrupt,
	}: Props = $props();

	// ─── Internal State ──────────────────────────────────────────────────────────

	let viewMode = $state<'orb' | 'transcript'>('orb');
	/**
	 * Pipeline mode selects between the legacy batch flow (record → fal.ai
	 * Whisper → Claude → fal.ai TTS) and the Pipecat realtime pipeline
	 * (WebRTC streaming Deepgram + Cartesia). Persisted in localStorage
	 * so the user's preference survives reload.
	 */
	let pipelineMode = $state<'batch' | 'realtime'>(
		typeof window !== 'undefined' &&
			window.localStorage?.getItem('ai-shuffle:voice-pipeline-mode') === 'realtime'
			? 'realtime'
			: 'batch'
	);
	$effect(() => {
		if (typeof window !== 'undefined') {
			window.localStorage?.setItem('ai-shuffle:voice-pipeline-mode', pipelineMode);
		}
	});
	let voiceState = $state<'idle' | 'listening' | 'processing' | 'speaking'>('idle');
	let audioLevel = $state(0);
	let isMuted = $state(false);
	let showSettings = $state(false);

	// Audio capture
	let mediaStream = $state<MediaStream | null>(null);
	let mediaRecorder = $state<MediaRecorder | null>(null);
	let audioContext = $state<AudioContext | null>(null);
	let analyserNode = $state<AnalyserNode | null>(null);
	let animationFrameId = $state<number | null>(null);
	let audioChunksRef: Blob[] = [];

	// Orb animation
	let orbRotation = $state(0);
	let orbAnimFrameId: number | null = null;
	let lastOrbTime = 0;

	// ─── Derived State ───────────────────────────────────────────────────────────

	const sessionTitle = $derived($activeSession.title || 'Voice Session');
	const isStreaming = $derived($activeSession.isStreaming);
	const hasMessages = $derived($activeSession.messages.length > 0);
	const voiceAudioUrl = $derived($activeSession.voiceResponseAudioUrl);

	// Audio playback element for TTS responses
	let ttsAudio: HTMLAudioElement | null = null;

	const stateLabel = $derived.by(() => {
		switch (voiceState) {
			case 'idle': return 'Tap the mic to speak';
			case 'listening': return 'Listening...';
			case 'processing': return 'Thinking...';
			case 'speaking': return 'Claude is speaking';
			default: return '';
		}
	});

	// Orb visual parameters derived from state + audio level
	const orbScale = $derived.by(() => {
		const base = 1.0;
		switch (voiceState) {
			case 'idle': return base + Math.sin(Date.now() / 1200) * 0.03;
			case 'listening': return base + audioLevel * 0.35;
			case 'processing': return base + 0.05;
			case 'speaking': return base + audioLevel * 0.25;
			default: return base;
		}
	});

	const orbGlowIntensity = $derived.by(() => {
		switch (voiceState) {
			case 'idle': return 0.15;
			case 'listening': return 0.3 + audioLevel * 0.5;
			case 'processing': return 0.4;
			case 'speaking': return 0.25 + audioLevel * 0.45;
			default: return 0.15;
		}
	});

	// ─── Orb Animation Loop ──────────────────────────────────────────────────────

	function startOrbAnimation() {
		lastOrbTime = performance.now();
		function tick(now: number) {
			const dt = (now - lastOrbTime) / 1000;
			lastOrbTime = now;

			// Rotate gradient for processing state
			if (voiceState === 'processing') {
				orbRotation = (orbRotation + dt * 120) % 360;
			} else {
				orbRotation = (orbRotation + dt * 15) % 360;
			}

			orbAnimFrameId = requestAnimationFrame(tick);
		}
		orbAnimFrameId = requestAnimationFrame(tick);
	}

	function stopOrbAnimation() {
		if (orbAnimFrameId !== null) {
			cancelAnimationFrame(orbAnimFrameId);
			orbAnimFrameId = null;
		}
	}

	// Start orb animation on mount
	startOrbAnimation();

	// ─── Audio Level Monitoring ──────────────────────────────────────────────────

	function startAudioLevelMonitoring(stream: MediaStream) {
		try {
			audioContext = new AudioContext();
			const source = audioContext.createMediaStreamSource(stream);
			analyserNode = audioContext.createAnalyser();
			analyserNode.fftSize = 256;
			analyserNode.smoothingTimeConstant = 0.6;
			source.connect(analyserNode);

			const dataArray = new Uint8Array(analyserNode.frequencyBinCount);

			function updateLevel() {
				if (!analyserNode) {
					audioLevel = 0;
					return;
				}
				analyserNode.getByteFrequencyData(dataArray);
				const average = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;
				audioLevel = Math.min(1, average / 100);
				animationFrameId = requestAnimationFrame(updateLevel);
			}
			updateLevel();
		} catch (e) {
			console.warn('[VoiceChatView] Audio level monitoring unavailable:', e);
		}
	}

	function stopAudioLevelMonitoring() {
		if (animationFrameId !== null) {
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

	// ─── Recording Controls ──────────────────────────────────────────────────────

	async function startRecording() {
		if (isMuted) return;
		audioChunksRef = [];

		try {
			if (!navigator.mediaDevices) {
				throw new Error('Microphone requires a secure context (HTTPS or localhost).');
			}
			const stream = await navigator.mediaDevices.getUserMedia({
				audio: {
					echoCancellation: true,
					noiseSuppression: true,
					autoGainControl: true,
				}
			});
			mediaStream = stream;

			const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
				? 'audio/webm;codecs=opus'
				: MediaRecorder.isTypeSupported('audio/webm')
					? 'audio/webm'
					: 'audio/mp4';

			const recorder = new MediaRecorder(stream, { mimeType });
			recorder.ondataavailable = (event) => {
				if (event.data.size > 0) {
					audioChunksRef.push(event.data);
				}
			};
			recorder.start(500);
			mediaRecorder = recorder;

			startAudioLevelMonitoring(stream);
			voiceState = 'listening';
		} catch (error) {
			console.error('[VoiceChatView] Failed to start recording:', error);
			voiceState = 'idle';
		}
	}

	async function stopRecording() {
		stopAudioLevelMonitoring();

		if (!mediaRecorder || mediaRecorder.state === 'inactive') {
			voiceState = 'idle';
			return;
		}

		const recorder = mediaRecorder;
		const stream = recorder.stream;
		const mimeType = recorder.mimeType;

		voiceState = 'processing';

		try {
			const audioBlob = await new Promise<Blob>((resolve, reject) => {
				const handleData = (event: BlobEvent) => {
					if (event.data.size > 0) {
						audioChunksRef.push(event.data);
					}
				};

				const handleStop = () => {
					recorder.removeEventListener('dataavailable', handleData);
					recorder.removeEventListener('stop', handleStop);
					stream.getTracks().forEach(track => track.stop());

					if (audioChunksRef.length === 0) {
						reject(new Error('No audio recorded'));
						return;
					}
					resolve(new Blob(audioChunksRef, { type: mimeType }));
				};

				recorder.addEventListener('dataavailable', handleData);
				recorder.addEventListener('stop', handleStop);
				recorder.requestData();
				recorder.stop();
			});

			mediaRecorder = null;
			mediaStream = null;

			if (onSpeechEnd) {
				onSpeechEnd(audioBlob);
			}
		} catch (error) {
			console.error('[VoiceChatView] Recording error:', error);
			voiceState = 'idle';
		}
	}

	function toggleRecording() {
		if (voiceState === 'listening') {
			stopRecording();
		} else if (voiceState === 'idle') {
			startRecording();
		} else if (voiceState === 'speaking') {
			// Interrupt AI speaking
			if (onInterrupt) onInterrupt();
			voiceState = 'idle';
		}
	}

	function toggleMute() {
		isMuted = !isMuted;
		if (isMuted && voiceState === 'listening') {
			stopRecording();
			voiceState = 'idle';
		}
	}

	function endSession() {
		stopRecording();
		stopAudioLevelMonitoring();
		voiceState = 'idle';
	}

	// ─── Sync with streaming state ──────────────────────────────────────────────

	$effect(() => {
		if (isStreaming && voiceState === 'processing') {
			voiceState = 'speaking';
		} else if (!isStreaming && voiceState === 'speaking') {
			voiceState = 'idle';
		}
	});

	// ─── Play TTS audio when response arrives ───────────────────────────────────

	$effect(() => {
		if (voiceAudioUrl) {
			playTtsAudio(voiceAudioUrl);
		}
	});

	function playTtsAudio(url: string) {
		// Stop any existing playback
		if (ttsAudio) {
			ttsAudio.pause();
			ttsAudio = null;
		}

		ttsAudio = new Audio(url);
		voiceState = 'speaking';

		ttsAudio.onended = () => {
			voiceState = 'idle';
			ttsAudio = null;
		};

		ttsAudio.onerror = () => {
			console.warn('[VoiceChatView] TTS audio playback failed');
			voiceState = 'idle';
			ttsAudio = null;
		};

		ttsAudio.play().catch((err) => {
			console.warn('[VoiceChatView] TTS play() rejected:', err);
			voiceState = 'idle';
		});
	}

	// ─── Cleanup ─────────────────────────────────────────────────────────────────

	onDestroy(() => {
		stopOrbAnimation();
		stopAudioLevelMonitoring();
		if (mediaRecorder && mediaRecorder.state !== 'inactive') {
			try {
				mediaRecorder.stop();
				mediaRecorder.stream.getTracks().forEach(track => track.stop());
			} catch { /* ignore */ }
		}
		if (ttsAudio) {
			ttsAudio.pause();
			ttsAudio = null;
		}
	});
</script>

<div class="voice-chat-view">
	{#if pipelineMode === 'realtime'}
		<!-- ═══════════════════ REALTIME (Pipecat) MODE ═══════════════════ -->
		<div class="realtime-wrap">
			<header class="realtime-header">
				<button
					class="mode-toggle"
					title="Switch to batch (legacy) mode"
					onclick={() => (pipelineMode = 'batch')}
				>
					<ChevronLeft size={16} />
					<span>Batch mode</span>
				</button>
				<div class="realtime-title">
					<Zap size={14} />
					Realtime voice
				</div>
			</header>
			<RealtimeVoicePanel
				projectId={$activeSession.projectId ?? undefined}
				sessionId={$activeSession.sessionId ?? undefined}
			/>
		</div>
	{:else if viewMode === 'orb'}
		<!-- ═══════════════════ ORB MODE ═══════════════════ -->
		<div class="orb-container">
			<!-- Ambient background grain -->
			<div class="ambient-grain"></div>

			<!-- Concentric rings (visible during listening) -->
			{#if voiceState === 'listening' || voiceState === 'speaking'}
				<div class="orb-rings">
					<div
						class="orb-ring orb-ring-1"
						style="transform: scale({1 + audioLevel * 0.6}); opacity: {0.08 + audioLevel * 0.12}"
					></div>
					<div
						class="orb-ring orb-ring-2"
						style="transform: scale({1 + audioLevel * 0.9}); opacity: {0.05 + audioLevel * 0.08}"
					></div>
					<div
						class="orb-ring orb-ring-3"
						style="transform: scale({1 + audioLevel * 1.2}); opacity: {0.03 + audioLevel * 0.05}"
					></div>
				</div>
			{/if}

			<!-- The Orb -->
			<div
				class="orb"
				class:orb-idle={voiceState === 'idle'}
				class:orb-listening={voiceState === 'listening'}
				class:orb-processing={voiceState === 'processing'}
				class:orb-speaking={voiceState === 'speaking'}
				style="
					transform: scale({orbScale});
					--orb-rotation: {orbRotation}deg;
					--glow-intensity: {orbGlowIntensity};
					--audio-level: {audioLevel};
				"
				role="status"
				aria-label={stateLabel}
			>
				<div class="orb-inner">
					<div class="orb-gradient"></div>
					<div class="orb-highlight"></div>
					{#if voiceState === 'processing'}
						<div class="orb-shimmer"></div>
					{/if}
				</div>
			</div>

			<!-- Session info below orb -->
			<div class="orb-info">
				<h2 class="orb-title">{sessionTitle}</h2>
				<p class="orb-state-label">{stateLabel}</p>
			</div>
		</div>

		<!-- Bottom controls bar -->
		<div class="voice-controls">
			<div class="controls-row">
				<!-- Mute button -->
				<button
					class="control-btn"
					class:control-btn-active={isMuted}
					onclick={toggleMute}
					title={isMuted ? 'Unmute' : 'Mute'}
					aria-label={isMuted ? 'Unmute microphone' : 'Mute microphone'}
				>
					{#if isMuted}
						<MicOff size={20} strokeWidth={1.75} />
					{:else}
						<Mic size={20} strokeWidth={1.75} />
					{/if}
				</button>

				<!-- Main mic button -->
				<button
					class="mic-btn"
					class:mic-btn-recording={voiceState === 'listening'}
					class:mic-btn-processing={voiceState === 'processing'}
					class:mic-btn-speaking={voiceState === 'speaking'}
					onclick={toggleRecording}
					disabled={isMuted && voiceState === 'idle'}
					title={voiceState === 'listening' ? 'Stop recording' : voiceState === 'speaking' ? 'Interrupt' : 'Start recording'}
					aria-label={voiceState === 'listening' ? 'Stop recording' : voiceState === 'speaking' ? 'Interrupt Claude' : 'Start recording'}
				>
					{#if voiceState === 'listening'}
						<!-- Waveform bars -->
						<div class="mic-waveform">
							<span class="mic-bar" style="height: {20 + audioLevel * 60}%"></span>
							<span class="mic-bar" style="height: {40 + audioLevel * 50}%"></span>
							<span class="mic-bar" style="height: {30 + audioLevel * 70}%"></span>
							<span class="mic-bar" style="height: {50 + audioLevel * 40}%"></span>
							<span class="mic-bar" style="height: {25 + audioLevel * 55}%"></span>
						</div>
					{:else if voiceState === 'processing'}
						<div class="mic-processing">
							<span class="mic-dot"></span>
							<span class="mic-dot"></span>
							<span class="mic-dot"></span>
						</div>
					{:else}
						<Mic size={28} strokeWidth={1.75} />
					{/if}
				</button>

				<!-- Transcript toggle -->
				<button
					class="control-btn"
					onclick={() => viewMode = 'transcript'}
					title="Show transcript"
					aria-label="Switch to transcript view"
				>
					<MessageSquareText size={20} strokeWidth={1.75} />
				</button>

				<!-- Realtime (Pipecat) toggle -->
				<button
					class="control-btn"
					onclick={() => (pipelineMode = 'realtime')}
					title="Switch to realtime voice (Pipecat)"
					aria-label="Switch to realtime voice"
				>
					<Zap size={20} strokeWidth={1.75} />
				</button>

				<!-- End session -->
				<button
					class="control-btn control-btn-danger"
					onclick={endSession}
					title="End voice session"
					aria-label="End voice session"
				>
					<PhoneOff size={20} strokeWidth={1.75} />
				</button>

				<!-- Settings -->
				<button
					class="control-btn"
					onclick={() => showSettings = !showSettings}
					title="Voice settings"
					aria-label="Voice settings"
				>
					<Settings size={20} strokeWidth={1.75} />
				</button>
			</div>
		</div>

	{:else}
		<!-- ═══════════════════ TRANSCRIPT MODE ═══════════════════ -->
		<div class="transcript-container">
			<div class="transcript-messages">
				<MessageArea />
			</div>

			<!-- Floating audio controls overlay -->
			<div class="transcript-controls">
				<!-- Audio level micro-indicator -->
				{#if voiceState === 'listening' || voiceState === 'speaking'}
					<div class="transcript-level-indicator">
						<div class="transcript-level-bar" style="height: {audioLevel * 100}%"></div>
					</div>
				{/if}

				<div class="transcript-controls-row">
					<!-- Back to orb mode -->
					<button
						class="control-btn control-btn-sm"
						onclick={() => viewMode = 'orb'}
						title="Back to orb view"
						aria-label="Switch to orb view"
					>
						<ChevronLeft size={18} strokeWidth={1.75} />
						<AudioWaveform size={18} strokeWidth={1.75} />
					</button>

					<!-- Main mic button (compact) -->
					<button
						class="mic-btn mic-btn-sm"
						class:mic-btn-recording={voiceState === 'listening'}
						class:mic-btn-processing={voiceState === 'processing'}
						onclick={toggleRecording}
						disabled={isMuted && voiceState === 'idle'}
						aria-label={voiceState === 'listening' ? 'Stop recording' : 'Start recording'}
					>
						{#if voiceState === 'listening'}
							<div class="mic-waveform mic-waveform-sm">
								<span class="mic-bar" style="height: {25 + audioLevel * 55}%"></span>
								<span class="mic-bar" style="height: {40 + audioLevel * 50}%"></span>
								<span class="mic-bar" style="height: {30 + audioLevel * 60}%"></span>
							</div>
						{:else}
							<Mic size={20} strokeWidth={1.75} />
						{/if}
					</button>

					<!-- Mute button -->
					<button
						class="control-btn control-btn-sm"
						class:control-btn-active={isMuted}
						onclick={toggleMute}
						aria-label={isMuted ? 'Unmute' : 'Mute'}
					>
						{#if isMuted}
							<MicOff size={18} strokeWidth={1.75} />
						{:else}
							<Mic size={18} strokeWidth={1.75} />
						{/if}
					</button>
				</div>
			</div>
		</div>
	{/if}
</div>

<style>
	/* ═══════════════════════════════════════════════════
	   BASE LAYOUT
	   ═══════════════════════════════════════════════════ */

	.voice-chat-view {
		position: relative;
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow: hidden;
		background: var(--background);
		color: var(--foreground);
	}

	/* ═══════════════════════════════════════════════════
	   REALTIME (Pipecat) MODE
	   ═══════════════════════════════════════════════════ */

	.realtime-wrap {
		display: flex;
		flex-direction: column;
		gap: 14px;
		padding: 16px;
		height: 100%;
		overflow-y: auto;
	}

	.realtime-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
	}

	.mode-toggle {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 6px 10px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 8px;
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		cursor: pointer;
	}

	.mode-toggle:hover {
		color: var(--foreground);
	}

	.realtime-title {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--foreground);
	}

	/* ═══════════════════════════════════════════════════
	   ORB MODE
	   ═══════════════════════════════════════════════════ */

	.orb-container {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		position: relative;
		overflow: hidden;
		gap: 2rem;
	}

	/* Subtle animated noise grain for depth */
	.ambient-grain {
		position: absolute;
		inset: 0;
		opacity: 0.03;
		background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
		pointer-events: none;
	}

	/* ─── Concentric Rings ─── */

	.orb-rings {
		position: absolute;
		display: flex;
		align-items: center;
		justify-content: center;
		pointer-events: none;
	}

	.orb-ring {
		position: absolute;
		border-radius: 50%;
		border: 1px solid var(--primary);
		transition: transform 0.12s ease-out, opacity 0.12s ease-out;
	}

	.orb-ring-1 {
		width: 220px;
		height: 220px;
	}

	.orb-ring-2 {
		width: 300px;
		height: 300px;
	}

	.orb-ring-3 {
		width: 380px;
		height: 380px;
	}

	@media (max-width: 480px) {
		.orb-ring-1 { width: 180px; height: 180px; }
		.orb-ring-2 { width: 240px; height: 240px; }
		.orb-ring-3 { width: 300px; height: 300px; }
	}

	/* ─── The Orb ─── */

	.orb {
		position: relative;
		width: 160px;
		height: 160px;
		border-radius: 50%;
		transition: transform 0.15s ease-out;
		will-change: transform;
	}

	@media (min-width: 768px) {
		.orb {
			width: 200px;
			height: 200px;
		}
	}

	.orb-inner {
		position: relative;
		width: 100%;
		height: 100%;
		border-radius: 50%;
		overflow: hidden;
	}

	/* Base gradient — rotates via custom property */
	.orb-gradient {
		position: absolute;
		inset: 0;
		border-radius: 50%;
		background: conic-gradient(
			from var(--orb-rotation, 0deg),
			var(--primary),
			color-mix(in oklch, var(--primary) 60%, oklch(0.5 0.2 300)),
			var(--primary),
			color-mix(in oklch, var(--primary) 70%, oklch(0.4 0.15 220)),
			var(--primary)
		);
		opacity: 0.85;
	}

	/* Top-left specular highlight */
	.orb-highlight {
		position: absolute;
		inset: 0;
		border-radius: 50%;
		background: radial-gradient(
			circle at 35% 30%,
			rgba(255, 255, 255, 0.25) 0%,
			rgba(255, 255, 255, 0.05) 40%,
			transparent 70%
		);
	}

	/* Processing shimmer overlay */
	.orb-shimmer {
		position: absolute;
		inset: -50%;
		border-radius: 50%;
		background: conic-gradient(
			from var(--orb-rotation, 0deg),
			transparent 0%,
			rgba(255, 255, 255, 0.15) 10%,
			transparent 20%
		);
		animation: shimmer-spin 2s linear infinite;
	}

	@keyframes shimmer-spin {
		to { transform: rotate(360deg); }
	}

	/* State-specific glow */
	.orb-idle {
		box-shadow:
			0 0 40px color-mix(in oklch, var(--primary) calc(var(--glow-intensity, 0.15) * 100%), transparent),
			0 0 80px color-mix(in oklch, var(--primary) calc(var(--glow-intensity, 0.15) * 50%), transparent);
		animation: orb-breathe 4s ease-in-out infinite;
	}

	.orb-listening {
		box-shadow:
			0 0 30px color-mix(in oklch, var(--primary) calc(var(--glow-intensity, 0.3) * 100%), transparent),
			0 0 60px color-mix(in oklch, var(--primary) calc(var(--glow-intensity, 0.3) * 60%), transparent),
			0 0 100px color-mix(in oklch, var(--primary) calc(var(--glow-intensity, 0.3) * 30%), transparent);
	}

	.orb-processing {
		box-shadow:
			0 0 40px color-mix(in oklch, oklch(0.7 0.15 280) calc(var(--glow-intensity, 0.4) * 100%), transparent),
			0 0 80px color-mix(in oklch, oklch(0.7 0.15 280) calc(var(--glow-intensity, 0.4) * 50%), transparent);
	}

	.orb-speaking {
		box-shadow:
			0 0 35px color-mix(in oklch, var(--primary) calc(var(--glow-intensity, 0.3) * 100%), transparent),
			0 0 70px color-mix(in oklch, var(--primary) calc(var(--glow-intensity, 0.3) * 50%), transparent);
	}

	@keyframes orb-breathe {
		0%, 100% { transform: scale(1); }
		50% { transform: scale(1.04); }
	}

	@media (prefers-reduced-motion: reduce) {
		.orb-idle {
			animation: none;
		}
		.orb-shimmer {
			animation: none;
		}
		.orb {
			transition: none;
		}
	}

	/* ─── Orb Info ─── */

	.orb-info {
		text-align: center;
		z-index: 1;
	}

	.orb-title {
		font-size: 1.125rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0;
		letter-spacing: -0.01em;
	}

	.orb-state-label {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		margin: 0.375rem 0 0;
		letter-spacing: 0.02em;
	}

	/* ═══════════════════════════════════════════════════
	   VOICE CONTROLS BAR (ORB MODE)
	   ═══════════════════════════════════════════════════ */

	.voice-controls {
		flex-shrink: 0;
		padding: 1rem 1rem 1.5rem;
		display: flex;
		justify-content: center;
	}

	.controls-row {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		padding: 0.625rem 1rem;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 9999px;
		box-shadow: var(--shadow-m, 0 4px 12px rgba(0,0,0,0.15));
	}

	/* Secondary control buttons */
	.control-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		border-radius: 50%;
		border: none;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 0.2s ease;
	}

	.control-btn:hover {
		background: var(--hover-overlay, color-mix(in oklch, var(--foreground) 8%, transparent));
		color: var(--foreground);
	}

	.control-btn:focus-visible {
		outline: 2px solid var(--primary);
		outline-offset: 2px;
	}

	.control-btn-active {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
	}

	.control-btn-active:hover {
		background: color-mix(in oklch, var(--destructive) 25%, transparent);
		color: var(--destructive);
	}

	.control-btn-danger:hover {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
	}

	.control-btn-sm {
		width: 40px;
		height: 40px;
		gap: 2px;
	}

	/* ─── Main Mic Button ─── */

	.mic-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 64px;
		height: 64px;
		border-radius: 50%;
		border: 2px solid var(--border);
		background: var(--card);
		color: var(--foreground);
		cursor: pointer;
		transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
		position: relative;
	}

	.mic-btn:hover:not(:disabled) {
		border-color: var(--primary);
		background: color-mix(in oklch, var(--primary) 10%, var(--card));
		box-shadow: 0 0 20px color-mix(in oklch, var(--primary) 20%, transparent);
	}

	.mic-btn:active:not(:disabled) {
		transform: scale(0.95);
	}

	.mic-btn:focus-visible {
		outline: 2px solid var(--primary);
		outline-offset: 3px;
	}

	.mic-btn:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	.mic-btn-recording {
		border-color: var(--destructive);
		background: color-mix(in oklch, var(--destructive) 15%, var(--card));
		color: var(--destructive);
		box-shadow: 0 0 24px color-mix(in oklch, var(--destructive) 25%, transparent);
	}

	.mic-btn-recording:hover:not(:disabled) {
		background: color-mix(in oklch, var(--destructive) 25%, var(--card));
		border-color: var(--destructive);
		box-shadow: 0 0 30px color-mix(in oklch, var(--destructive) 35%, transparent);
	}

	.mic-btn-processing {
		border-color: color-mix(in oklch, var(--primary) 50%, oklch(0.7 0.15 280));
		color: var(--muted-foreground);
		cursor: default;
	}

	.mic-btn-speaking {
		border-color: var(--primary);
		background: color-mix(in oklch, var(--primary) 10%, var(--card));
	}

	.mic-btn-sm {
		width: 48px;
		height: 48px;
	}

	/* Waveform inside mic button */
	.mic-waveform {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 3px;
		height: 28px;
	}

	.mic-waveform-sm {
		height: 20px;
		gap: 2px;
	}

	.mic-bar {
		width: 3px;
		min-height: 4px;
		background: currentColor;
		border-radius: 1.5px;
		transition: height 0.08s ease-out;
	}

	/* Processing dots inside mic button */
	.mic-processing {
		display: flex;
		align-items: center;
		gap: 5px;
	}

	.mic-dot {
		width: 6px;
		height: 6px;
		background: currentColor;
		border-radius: 50%;
		animation: mic-dot-bounce 1.2s infinite ease-in-out;
	}

	.mic-dot:nth-child(1) { animation-delay: 0s; }
	.mic-dot:nth-child(2) { animation-delay: 0.2s; }
	.mic-dot:nth-child(3) { animation-delay: 0.4s; }

	@keyframes mic-dot-bounce {
		0%, 80%, 100% {
			transform: scale(0.7);
			opacity: 0.4;
		}
		40% {
			transform: scale(1.1);
			opacity: 1;
		}
	}

	/* ═══════════════════════════════════════════════════
	   TRANSCRIPT MODE
	   ═══════════════════════════════════════════════════ */

	.transcript-container {
		flex: 1;
		display: flex;
		flex-direction: column;
		min-height: 0;
		position: relative;
	}

	.transcript-messages {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}

	/* Floating audio controls at bottom */
	.transcript-controls {
		position: absolute;
		bottom: 1rem;
		left: 50%;
		transform: translateX(-50%);
		z-index: 20;
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.transcript-controls-row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.5rem 0.75rem;
		background: var(--glass-bg, color-mix(in oklch, var(--card) 90%, transparent));
		border: 1px solid var(--glass-border, var(--border));
		border-radius: 9999px;
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
		box-shadow: var(--shadow-l, 0 8px 24px rgba(0,0,0,0.2));
	}

	/* Micro audio level indicator */
	.transcript-level-indicator {
		width: 4px;
		height: 32px;
		border-radius: 2px;
		background: var(--muted);
		overflow: hidden;
		position: relative;
	}

	.transcript-level-bar {
		position: absolute;
		bottom: 0;
		left: 0;
		right: 0;
		background: var(--primary);
		border-radius: 2px;
		transition: height 0.08s ease-out;
	}

	/* ═══════════════════════════════════════════════════
	   RESPONSIVE
	   ═══════════════════════════════════════════════════ */

	@media (max-width: 480px) {
		.orb {
			width: 140px;
			height: 140px;
		}

		.mic-btn {
			width: 56px;
			height: 56px;
		}

		.control-btn {
			width: 40px;
			height: 40px;
		}

		.controls-row {
			gap: 0.5rem;
			padding: 0.5rem 0.75rem;
		}
	}

	@media (min-width: 1024px) {
		.orb {
			width: 220px;
			height: 220px;
		}

		.orb-ring-1 { width: 280px; height: 280px; }
		.orb-ring-2 { width: 380px; height: 380px; }
		.orb-ring-3 { width: 480px; height: 480px; }

		.mic-btn {
			width: 72px;
			height: 72px;
		}
	}
</style>
