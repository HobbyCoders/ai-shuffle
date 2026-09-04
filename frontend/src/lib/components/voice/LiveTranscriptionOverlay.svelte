<script lang="ts">
	/**
	 * LiveTranscriptionOverlay — the floating console for global voice input.
	 *
	 * Replaces the old status pill. On the live path it shows text arriving while
	 * the user is still speaking: the in-flight segment shimmers, then sets into
	 * solid type when the model finalises it. On the batch path (fal models) the
	 * same shell degrades to the meter and a "transcribing" state, so there is
	 * one voice-input surface rather than two.
	 *
	 * The waveform reads `globalSttAudio`, a separate store, so 60 fps meter
	 * updates never re-render the transcript.
	 */
	import { fly } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';
	import {
		globalSttState,
		globalSttAudio,
		globalSttUpload,
		clearError,
		cancelVoiceInput
	} from '$lib/services/globalStt';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences.svelte';
	import { formatHotkeyConfig } from '$lib/services/keyboard';

	const isVisible = $derived(
		$globalSttState.isPreparing ||
			$globalSttState.isRecording ||
			$globalSttState.isTranscribing ||
			$globalSttState.error !== null
	);

	const hotkeyDisplay = $derived(formatHotkeyConfig($hotkeyPreferences.globalSttHotkey));

	const isLive = $derived($globalSttState.mode === 'live');
	const hasError = $derived($globalSttState.error !== null);

	// The transcript panel only exists once there is something to put in it —
	// otherwise the overlay stays a compact pill.
	const hasTranscript = $derived(
		$globalSttState.segments.length > 0 || $globalSttState.partial.length > 0
	);

	function formatClock(ms: number): string {
		const total = Math.round(ms / 1000);
		return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
	}

	const statusLabel = $derived.by(() => {
		if ($globalSttState.error) return 'Voice input failed';
		if ($globalSttState.isPreparing) return isLive ? 'Connecting…' : 'Starting microphone…';
		if ($globalSttState.isTranscribing) {
			if (isLive) return 'Finishing up…';
			// Naming the clip length turns an unexplained wait into a wait with a
			// reason — three minutes of audio takes a while, and silence about it
			// is what made this feel broken.
			const ms = $globalSttState.recordingMs;
			return ms ? `Transcribing ${formatClock(ms)} of audio…` : 'Transcribing…';
		}
		if ($globalSttState.speechActive) return 'Hearing you';
		return 'Listening';
	});

	// Upload is the leg worth reporting: it is what actually takes time on a
	// phone. Once the bytes are up we stop claiming to know how far along we are.
	const uploadPercent = $derived(
		$globalSttUpload !== null && $globalSttUpload < 1 ? Math.round($globalSttUpload * 100) : null
	);

	const canCancel = $derived(
		!hasError &&
			($globalSttState.isPreparing ||
				$globalSttState.isRecording ||
				$globalSttState.isTranscribing)
	);

	// Idle bars breathe; active bars follow the spectrum. Splitting the two keeps
	// a silent microphone from looking like a frozen UI.
	const isQuiet = $derived($globalSttState.isRecording && $globalSttAudio.level < 0.025);

	// The glow tracks loudness, clamped so a shout doesn't wash out the screen.
	const glow = $derived(Math.min($globalSttAudio.level * 2.4, 1));

	let transcriptEl = $state<HTMLDivElement | null>(null);

	// Keep the newest words in view as the transcript grows past the panel.
	$effect(() => {
		// Touch both so the effect re-runs on either kind of text change.
		void $globalSttState.partial;
		void $globalSttState.segments.length;
		if (transcriptEl) {
			transcriptEl.scrollTop = transcriptEl.scrollHeight;
		}
	});

	$effect(() => {
		if ($globalSttState.error) {
			const timer = setTimeout(clearError, 6000);
			return () => clearTimeout(timer);
		}
	});

	$effect(() => {
		if (!canCancel) return;
		const onKey = (event: KeyboardEvent) => {
			if (event.key !== 'Escape') return;
			event.stopPropagation();
			event.preventDefault();
			cancelVoiceInput();
		};
		window.addEventListener('keydown', onKey, true);
		return () => window.removeEventListener('keydown', onKey, true);
	});
</script>

{#if isVisible}
	<div
		class="overlay"
		class:live={isLive}
		class:error={hasError}
		class:speaking={$globalSttState.speechActive}
		class:finishing={$globalSttState.isTranscribing}
		style="--glow: {glow}"
		role="status"
		aria-live="polite"
		transition:fly={{ y: 24, duration: 260, easing: cubicOut }}
	>
		<!-- Transcript grows the panel open. The 0fr/1fr grid row is what makes
		     the height animate smoothly without measuring anything. -->
		<div class="transcript-wrap" class:open={hasTranscript && !hasError}>
			<div class="transcript-clip">
				<div class="transcript" bind:this={transcriptEl}>
					{#each $globalSttState.segments as segment, i (i)}
						<span class="segment">{segment} </span>
					{/each}
					{#if $globalSttState.partial}
						<span class="partial">{$globalSttState.partial}</span>
					{/if}
					<span class="caret" aria-hidden="true"></span>
				</div>
			</div>
		</div>

		<div class="bar">
			{#if hasError}
				<svg class="glyph error-glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<circle cx="12" cy="12" r="10" />
					<line x1="12" y1="8" x2="12" y2="13" />
					<line x1="12" y1="16.5" x2="12" y2="16.6" />
				</svg>
				<span class="status error-text">{$globalSttState.error}</span>
				<button class="dismiss" onclick={clearError} aria-label="Dismiss error">
					<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<line x1="18" y1="6" x2="6" y2="18" />
						<line x1="6" y1="6" x2="18" y2="18" />
					</svg>
				</button>
			{:else}
				<div class="orb" class:pulsing={$globalSttState.isPreparing}>
					<span class="orb-ring" style="transform: scale({1 + glow * 0.55})"></span>
					<span class="orb-core"></span>
				</div>

				<div class="wave" class:quiet={isQuiet} aria-hidden="true">
					{#each $globalSttAudio.bands as band, i (i)}
						<span
							class="wave-bar"
							style="--h: {Math.max(0.12, Math.min(1, band * 1.65))}; --i: {i}"
						></span>
					{/each}
				</div>

				<div class="meta">
					<span class="status">{statusLabel}</span>
					{#if uploadPercent !== null}
						<span class="hint">{uploadPercent}% uploaded</span>
					{:else if $globalSttState.isRecording && !hasTranscript}
						<span class="hint">
							<kbd>{hotkeyDisplay}</kbd> to stop
						</span>
					{:else if isLive && $globalSttState.modelName}
						<span class="hint model">{$globalSttState.modelName}</span>
					{/if}
				</div>

				{#if canCancel}
					<button class="cancel" onclick={cancelVoiceInput} title="Discard (Esc)">
						Cancel
					</button>
				{/if}
			{/if}
		</div>

		{#if $globalSttUpload !== null}
			<div class="progress" class:indeterminate={$globalSttUpload >= 1}>
				<div class="progress-fill" style="width: {Math.max(2, $globalSttUpload * 100)}%"></div>
			</div>
		{/if}
	</div>
{/if}

<style>
	.overlay {
		position: fixed;
		bottom: 20px;
		left: 50%;
		translate: -50% 0;
		z-index: 9999;

		width: max-content;
		max-width: min(680px, calc(100vw - 32px));

		background: color-mix(in srgb, var(--popover) 88%, transparent);
		border: 1px solid var(--border);
		border-radius: 16px;
		backdrop-filter: blur(20px) saturate(1.4);
		-webkit-backdrop-filter: blur(20px) saturate(1.4);
		overflow: hidden;

		box-shadow:
			0 18px 48px -18px rgb(0 0 0 / 0.45),
			0 2px 8px -2px rgb(0 0 0 / 0.18);

		transition:
			border-color 240ms ease,
			box-shadow 240ms ease;
	}

	.overlay.live {
		border-color: color-mix(in srgb, var(--primary) 32%, var(--border));
	}

	/* The glow breathes with the speaker's volume rather than on a timer, so the
	   panel feels attached to the voice instead of merely animated. */
	.overlay.speaking {
		border-color: color-mix(in srgb, var(--primary) 58%, var(--border));
		box-shadow:
			0 18px 48px -18px rgb(0 0 0 / 0.45),
			0 0 calc(18px + var(--glow) * 34px)
				color-mix(in srgb, var(--primary) calc(14% + var(--glow) * 22%), transparent);
	}

	.overlay.error {
		border-color: var(--destructive);
		background: color-mix(in srgb, var(--destructive) 10%, var(--popover));
	}

	/* ── Transcript ──────────────────────────────────────────────────────── */

	.transcript-wrap {
		display: grid;
		grid-template-rows: 0fr;
		transition: grid-template-rows 320ms cubic-bezier(0.22, 1, 0.36, 1);
	}

	.transcript-wrap.open {
		grid-template-rows: 1fr;
	}

	.transcript-clip {
		overflow: hidden;
	}

	.transcript {
		max-height: 168px;
		overflow-y: auto;
		padding: 14px 18px 4px;
		font-size: 15px;
		line-height: 1.55;
		letter-spacing: -0.005em;
		scrollbar-width: thin;
		scrollbar-color: var(--border-strong, var(--border)) transparent;
	}

	.transcript::-webkit-scrollbar {
		width: 6px;
	}

	.transcript::-webkit-scrollbar-thumb {
		background: var(--border-strong, var(--border));
		border-radius: 3px;
	}

	/* A finalised segment lands once, then never moves again. */
	.segment {
		color: var(--foreground);
		animation: settle 360ms cubic-bezier(0.22, 1, 0.36, 1) both;
	}

	/* The in-flight segment reads as provisional: lighter, and lit by a slow
	   sweep so it is visibly still being written. */
	.partial {
		/* The sweep peaks below full strength on purpose: even at its brightest a
		   partial has to read lighter than the settled text beside it. */
		background: linear-gradient(
			100deg,
			color-mix(in srgb, var(--foreground) 38%, transparent) 0%,
			color-mix(in srgb, var(--foreground) 76%, transparent) 42%,
			color-mix(in srgb, var(--foreground) 38%, transparent) 78%
		);
		background-size: 260% 100%;
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
		-webkit-text-fill-color: transparent;
		animation: sweep 2.6s linear infinite;
	}

	.caret {
		display: inline-block;
		width: 2px;
		height: 1.05em;
		margin-left: 2px;
		vertical-align: text-bottom;
		background: var(--primary);
		border-radius: 1px;
		animation: blink 1.1s steps(1, end) infinite;
	}

	/* ── Bar ─────────────────────────────────────────────────────────────── */

	.bar {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 11px 16px;
	}

	.orb {
		position: relative;
		display: grid;
		place-items: center;
		width: 22px;
		height: 22px;
		flex-shrink: 0;
	}

	.orb-core {
		width: 9px;
		height: 9px;
		border-radius: 50%;
		background: var(--primary);
		box-shadow: 0 0 10px color-mix(in srgb, var(--primary) 70%, transparent);
	}

	.orb-ring {
		position: absolute;
		inset: 0;
		border: 1.5px solid color-mix(in srgb, var(--primary) 45%, transparent);
		border-radius: 50%;
		transition: transform 110ms cubic-bezier(0.22, 1, 0.36, 1);
	}

	.orb.pulsing .orb-core {
		animation: pulse 1.4s ease-in-out infinite;
	}

	.orb.pulsing .orb-ring {
		animation: ripple 1.4s ease-out infinite;
	}

	/* Fixed width, not flexible: the bars cap at 4px each, so letting the strip
	   stretch with the panel would only pool dead space beside them. */
	.wave {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 2px;
		height: 26px;
		width: 176px;
		flex: 0 0 auto;
	}

	.wave-bar {
		flex: 1 1 0;
		min-width: 2px;
		max-width: 4px;
		height: calc(var(--h) * 100%);
		border-radius: 999px;
		background: linear-gradient(
			var(--primary),
			color-mix(in srgb, var(--primary) 55%, transparent)
		);
		/* Fast enough to track speech, slow enough to smooth the FFT jitter. */
		transition: height 90ms cubic-bezier(0.22, 1, 0.36, 1);
	}

	/* Silence gets a slow travelling swell instead of a flat line, so the user
	   can tell the microphone is live before they say anything. */
	.wave.quiet .wave-bar {
		height: 22%;
		background: var(--muted-foreground);
		animation: breathe 2.1s ease-in-out infinite;
		animation-delay: calc(var(--i) * 42ms);
	}

	/* Working, not waiting. Faster and more directional than the idle breath, so
	   "transcribing" cannot be mistaken for "listening to silence" — before this
	   the two states rendered identically apart from one word of label. */
	.overlay.finishing .wave-bar {
		background: var(--muted-foreground);
		height: 24%;
		animation: convey 1.1s ease-in-out infinite;
		animation-delay: calc(var(--i) * 28ms);
	}

	.cancel {
		flex-shrink: 0;
		padding: 4px 10px;
		background: transparent;
		border: 1px solid var(--border);
		border-radius: 8px;
		color: var(--muted-foreground);
		font-size: 12px;
		font-weight: 500;
		cursor: pointer;
		transition:
			color 150ms ease,
			background 150ms ease,
			border-color 150ms ease;
	}

	.cancel:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
		border-color: var(--border-strong, var(--border));
	}

	.progress {
		height: 2px;
		background: color-mix(in srgb, var(--muted-foreground) 22%, transparent);
		overflow: hidden;
	}

	.progress-fill {
		height: 100%;
		background: var(--primary);
		transition: width 180ms ease-out;
	}

	/* The bytes are up; the model's own time is not ours to predict, so the bar
	   stops advancing and just shows that work continues. */
	.progress.indeterminate .progress-fill {
		width: 40% !important;
		animation: slide 1.2s ease-in-out infinite;
	}

	.meta {
		display: flex;
		align-items: baseline;
		gap: 8px;
		flex-shrink: 0;
		margin-left: auto;
	}

	.status {
		font-size: 13px;
		font-weight: 500;
		color: var(--foreground);
		white-space: nowrap;
	}

	.error-text {
		color: var(--destructive);
		font-weight: 400;
		white-space: normal;
		max-width: 380px;
	}

	.hint {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-size: 11px;
		color: var(--muted-foreground);
		white-space: nowrap;
	}

	.hint.model {
		font-family: var(--font-mono);
		font-size: 10px;
		opacity: 0.75;
	}

	kbd {
		padding: 1px 5px;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: 4px;
		font-family: var(--font-mono);
		font-size: 10px;
		font-weight: 600;
		color: var(--muted-foreground);
	}

	.glyph {
		width: 18px;
		height: 18px;
		flex-shrink: 0;
	}

	.error-glyph {
		color: var(--destructive);
	}

	.dismiss {
		display: grid;
		place-items: center;
		width: 20px;
		height: 20px;
		margin-left: auto;
		padding: 0;
		background: transparent;
		border: none;
		border-radius: 4px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition:
			color 150ms ease,
			background 150ms ease;
	}

	.dismiss:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	.dismiss svg {
		width: 12px;
		height: 12px;
	}

	/* ── Keyframes ───────────────────────────────────────────────────────── */

	@keyframes settle {
		from {
			opacity: 0;
			filter: blur(3px);
			transform: translateY(3px);
		}
		to {
			opacity: 1;
			filter: blur(0);
			transform: translateY(0);
		}
	}

	@keyframes sweep {
		from {
			background-position: 160% 0;
		}
		to {
			background-position: -60% 0;
		}
	}

	@keyframes blink {
		0%,
		50% {
			opacity: 1;
		}
		50.01%,
		100% {
			opacity: 0;
		}
	}

	@keyframes breathe {
		0%,
		100% {
			height: 16%;
			opacity: 0.5;
		}
		50% {
			height: 54%;
			opacity: 0.95;
		}
	}

	@keyframes convey {
		0%,
		100% {
			height: 18%;
			opacity: 0.35;
		}
		50% {
			height: 62%;
			opacity: 0.9;
		}
	}

	@keyframes slide {
		0% {
			transform: translateX(-100%);
		}
		100% {
			transform: translateX(250%);
		}
	}

	@keyframes pulse {
		0%,
		100% {
			transform: scale(0.82);
			opacity: 0.65;
		}
		50% {
			transform: scale(1.1);
			opacity: 1;
		}
	}

	@keyframes ripple {
		0% {
			transform: scale(0.7);
			opacity: 0.8;
		}
		100% {
			transform: scale(1.5);
			opacity: 0;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.segment,
		.partial,
		.caret,
		.orb.pulsing .orb-core,
		.orb.pulsing .orb-ring,
		.wave.quiet .wave-bar,
		.overlay.finishing .wave-bar,
		.progress.indeterminate .progress-fill {
			animation: none;
		}

		.partial {
			color: var(--muted-foreground);
			-webkit-text-fill-color: var(--muted-foreground);
		}

		.wave-bar,
		.orb-ring,
		.transcript-wrap {
			transition: none;
		}
	}
</style>
