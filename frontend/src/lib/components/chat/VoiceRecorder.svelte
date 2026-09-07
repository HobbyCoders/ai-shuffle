<script lang="ts">
	/**
	 * VoiceRecorder — the composer's microphone control.
	 *
	 * A trigger, not a recorder: capture, streaming and transcription all live in
	 * the shared stack (`globalStt`), which the global hotkey drives too. What
	 * this owns is the affordance — the button grows into a live waveform while
	 * you talk, and shrinks back when it finishes.
	 *
	 * For composer dictation, live text is written
	 * straight into the composer through the `live` sink below, so the editor IS
	 * the feedback, and the recording ends on its own when you stop speaking.
	 */
	import { onDestroy } from 'svelte';
	import Banner from '$lib/components/Banner.svelte';
	import {
		globalSttState,
		globalSttAudio,
		globalSttUpload,
		clearError,
		toggleVoiceInput,
		cancelVoiceInput
	} from '$lib/services/globalStt';

	interface Props {
		/** Final text, for the batch path and as the settle call on the live one. */
		onTranscription: (text: string) => void;
		/** Running dictation while the user is still speaking. Called with the
		 *  FULL text each time — replace, do not append. */
		onLiveText?: (text: string) => void;
		/** Dictation finished; release whatever span `onLiveText` was writing to. */
		onLiveCommit?: () => void;
		onLiveDiscard?: () => void;
		/** Read synchronously so navigation invalidates callbacks before effects run. */
		getOwner?: () => unknown;
		disabled?: boolean;
		isRecording?: boolean;
		isTranscribing?: boolean;
	}

	let {
		onTranscription,
		onLiveText,
		onLiveCommit,
		onLiveDiscard,
		getOwner,
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
	const expanded = $derived(isRecording || isTranscribing || isPreparing);

	// The meter publishes 28 bands; the button has room for far fewer, so take an
	// even spread rather than the first N — otherwise it would show only bass.
	const BAR_COUNT = 11;
	const bars = $derived.by(() => {
		const bands = $globalSttAudio.bands;
		if (!bands.length) return new Array(BAR_COUNT).fill(0);
		const step = bands.length / BAR_COUNT;
		return Array.from({ length: BAR_COUNT }, (_, i) => {
			const start = Math.floor(i * step);
			const end = Math.max(start + 1, Math.floor((i + 1) * step));
			let peak = 0;
			for (let j = start; j < end && j < bands.length; j++) {
				if (bands[j] > peak) peak = bands[j];
			}
			return peak;
		});
	});

	function toggleRecording() {
		if (isRecording) {
			toggleVoiceInput(onTranscription);
			return;
		}
		const owner = getOwner?.();
		const isCurrent = () => !getOwner || getOwner() === owner;
		toggleVoiceInput((text) => { if (isCurrent()) onTranscription(text); }, onLiveText ? {
			onText: (text) => { if (isCurrent()) onLiveText?.(text); },
			onCommit: () => { if (isCurrent()) onLiveCommit?.(); },
			onDiscard: () => { if (isCurrent()) onLiveDiscard?.(); }
		} : undefined);
	}

	function onCancel(event: MouseEvent) {
		event.stopPropagation();
		cancelVoiceInput();
	}

	// Escape discards. Bound on the window in the capture phase rather than
	// through registerShortcut: that registry is keyed by combo, so claiming
	// 'Escape' would evict the app's close-modal binding and unregistering would
	// delete it outright.
	$effect(() => {
		if (!expanded || $globalSttState.source !== 'composer') return;
		const onKey = (event: KeyboardEvent) => {
			if (event.key !== 'Escape') return;
			event.stopPropagation();
			event.preventDefault();
			cancelVoiceInput();
		};
		window.addEventListener('keydown', onKey, true);
		return () => window.removeEventListener('keydown', onKey, true);
	});

	// A recording left running when the composer goes away would hold the
	// microphone open with nowhere to put its text. Navigation keeps words
	// already visible in the draft; only the explicit Discard action removes them.
	onDestroy(() => {
		const state = $globalSttState;
		if (state.source === 'composer' && (state.isRecording || state.isPreparing || state.isTranscribing)) {
			cancelVoiceInput({ discard: false });
		}
	});
</script>

{#if $globalSttState.source === 'composer' && $globalSttState.error}
	<Banner
		severity="error"
		message={$globalSttState.error}
		dismissible
		onDismiss={clearError}
	/>
{/if}

<!-- Voice control is rendered inline within the send-button-wrapper -->
<div class="voice-btn-wrapper" class:expanded>
	<button
		type="button"
		onclick={toggleRecording}
		disabled={disabled || isTranscribing || isPreparing}
		class="voice-btn"
		class:preparing={isPreparing}
		class:recording={isRecording}
		class:transcribing={isTranscribing}
		class:expanded
		title={isPreparing
			? 'Starting…'
			: isRecording
				? 'Listening — stops on its own when you finish'
				: isTranscribing
					? 'Transcribing…'
					: 'Voice input'}
		aria-label={isRecording ? 'Stop voice input' : 'Start voice input'}
		style="--audio-level: {audioLevel}; --upload: {($globalSttUpload ?? 0) * 100}%"
	>
		<div class="voice-btn-content">
			{#if isRecording}
				<!-- The waveform IS the button while recording: no separate overlay,
				     nothing to look away from the composer for. -->
				<div class="voice-waveform" aria-hidden="true">
					{#each bars as bar, i (i)}
						<span
							class="waveform-bar"
							style="height: {Math.round(Math.max(0.14, Math.min(1, bar * 1.7)) * 100)}%"
						></span>
					{/each}
				</div>
			{:else if isTranscribing}
				<div class="voice-processing" aria-hidden="true">
					<span class="processing-dot"></span>
					<span class="processing-dot"></span>
					<span class="processing-dot"></span>
				</div>
			{:else if isPreparing}
				<svg class="voice-icon voice-icon-preparing" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" />
				</svg>
			{:else}
				<svg class="voice-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" />
				</svg>
			{/if}
		</div>
	</button>

	{#if isRecording || isTranscribing}
		<!-- Discard. Stopping is the recording's own job, so this button is only
		     ever the destructive one — which is why it is not the big target. -->
		<button
			type="button"
			class="voice-cancel"
			onclick={onCancel}
			title="Discard (Esc)"
			aria-label="Discard voice input"
		>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
				<line x1="18" y1="6" x2="6" y2="18" />
				<line x1="6" y1="6" x2="18" y2="18" />
			</svg>
		</button>
	{/if}
</div>

<style>
	/* ===== VOICE CONTROL ===== */
	.voice-btn-wrapper {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 2px;
		border-radius: min(50%, calc(var(--radius, 0.75rem) * 2.67));
	}

	/* Expanded, the wrapper holds a pill and must not clip it to a circle. */
	.voice-btn-wrapper.expanded {
		border-radius: 999px;
	}

	.voice-btn {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		padding: 0;
		background: transparent;
		border: none;
		border-radius: min(50%, calc(var(--radius, 0.75rem) * 2.67));
		cursor: pointer;
		color: var(--muted-foreground);
		overflow: hidden;
		/* Width is the animated property: the control grows out of the icon
		   rather than swapping to a different element. */
		transition:
			width 260ms cubic-bezier(0.22, 1, 0.36, 1),
			border-radius 260ms cubic-bezier(0.22, 1, 0.36, 1),
			background 180ms ease,
			color 180ms ease,
			box-shadow 180ms ease;
	}

	.voice-btn.expanded {
		width: 96px;
		border-radius: 999px;
	}

	.voice-btn:hover:not(:disabled) {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.voice-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.voice-btn.preparing {
		width: 52px;
		background: var(--status-warning-bg);
		color: var(--warning);
	}

	.voice-btn.preparing .voice-icon-preparing {
		animation: voice-pulse 1s ease-in-out infinite;
	}

	/* Recording: the live state, tinted and lit in proportion to loudness. */
	.voice-btn.recording {
		background: var(--status-error-bg);
		color: var(--destructive);
		box-shadow: 0 0 0 1px var(--status-error-border),
			0 0 calc(4px + var(--audio-level) * 14px)
				color-mix(in srgb, var(--destructive) calc(20% + var(--audio-level) * 40%), transparent);
	}

	/* Transcribing fills left-to-right with real upload progress, so a long clip
	   on a slow connection shows movement instead of three dots and hope. */
	.voice-btn.transcribing {
		width: 52px;
		color: var(--primary);
		background:
			linear-gradient(
				to right,
				color-mix(in srgb, var(--primary) 26%, transparent) var(--upload, 0%),
				transparent var(--upload, 0%)
			),
			var(--chip-primary-bg);
		transition:
			width 260ms cubic-bezier(0.22, 1, 0.36, 1),
			background 220ms ease-out,
			color 180ms ease;
	}

	.voice-btn-content {
		position: relative;
		z-index: 2;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 100%;
		height: 100%;
	}

	.voice-icon {
		width: 15px;
		height: 15px;
		flex-shrink: 0;
		transition: transform 0.2s ease;
	}

	/* Waveform */
	.voice-waveform {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 2px;
		width: 100%;
		height: 14px;
		padding: 0 9px;
	}

	.waveform-bar {
		flex: 1 1 0;
		min-width: 2px;
		max-width: 3px;
		background: currentColor;
		border-radius: 999px;
		/* Fast enough to feel attached to the voice, slow enough to smooth the
		   frame-to-frame jitter of the FFT. */
		transition: height 80ms cubic-bezier(0.22, 1, 0.36, 1);
	}

	/* Processing dots */
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

	/* Discard */
	.voice-cancel {
		display: grid;
		place-items: center;
		width: 20px;
		height: 20px;
		flex-shrink: 0;
		padding: 0;
		background: transparent;
		border: none;
		border-radius: 50%;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 150ms ease, background 150ms ease;
		animation: cancel-in 200ms cubic-bezier(0.22, 1, 0.36, 1) both;
	}

	.voice-cancel:hover {
		color: var(--destructive);
		background: var(--hover-overlay);
	}

	.voice-cancel svg {
		width: 10px;
		height: 10px;
	}

	.voice-btn:active:not(:disabled) {
		transform: scale(0.95);
	}

	@keyframes voice-pulse {
		0%, 100% { transform: scale(1); opacity: 0.7; }
		50% { transform: scale(1.1); opacity: 1; }
	}

	@keyframes processing-bounce {
		0%, 80%, 100% { transform: scale(0.8); opacity: 0.5; }
		40% { transform: scale(1.2); opacity: 1; }
	}

	@keyframes cancel-in {
		from { opacity: 0; transform: scale(0.6); }
		to { opacity: 1; transform: scale(1); }
	}

	@media (prefers-reduced-motion: reduce) {
		.voice-btn,
		.waveform-bar {
			transition: none;
		}
		.voice-cancel,
		.processing-dot,
		.voice-btn.preparing .voice-icon-preparing {
			animation: none;
		}
	}
</style>
