<script lang="ts">
	/**
	 * VoiceRecorder — the composer's microphone button.
	 *
	 * A trigger, not a recorder. Capture, live streaming, transcription and the
	 * on-screen transcript all live in the shared voice-input stack
	 * (`globalStt` + `LiveTranscriptionOverlay`), which the hotkey uses too. This
	 * component contributes the button and its meter, and names where the
	 * finished text should land — the editor, rather than the clipboard.
	 *
	 * Delegating is what gives the composer live transcription: it previously ran
	 * its own MediaRecorder and could only upload a finished clip.
	 */
	import {
		globalSttState,
		globalSttAudio,
		toggleVoiceInput
	} from '$lib/services/globalStt';

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

	// Mirror the shared state onto the bindable props ChatInput already reads.
	$effect(() => {
		isRecording = $globalSttState.isRecording;
	});
	$effect(() => {
		isTranscribing = $globalSttState.isTranscribing;
	});

	const isPreparing = $derived($globalSttState.isPreparing);
	const audioLevel = $derived($globalSttAudio.level);

	function toggleRecording() {
		toggleVoiceInput(onTranscription);
	}
</script>

<!-- Voice button is rendered inline within the send-button-wrapper -->
<div class="voice-btn-wrapper">
	<button
		type="button"
		onclick={toggleRecording}
		disabled={disabled || isTranscribing || isPreparing}
		class="voice-btn"
		class:preparing={isPreparing}
		class:recording={isRecording}
		class:transcribing={isTranscribing}
		title={isPreparing ? 'Starting...' : isRecording ? 'Tap to stop' : isTranscribing ? 'Transcribing...' : 'Voice input'}
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
