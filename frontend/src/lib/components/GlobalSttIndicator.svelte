<script lang="ts">
	/**
	 * GlobalSttIndicator - Floating overlay showing global STT recording state
	 *
	 * Shows at bottom-center of screen when:
	 * - Preparing to record
	 * - Recording (with audio level visualization)
	 * - Transcribing (with processing animation)
	 */
	import { globalSttState, clearError } from '$lib/services/globalStt';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences';
	import { formatHotkeyConfig } from '$lib/services/keyboard';

	// Derived visibility
	const isVisible = $derived(
		$globalSttState.isPreparing ||
		$globalSttState.isRecording ||
		$globalSttState.isTranscribing ||
		$globalSttState.error !== null
	);

	// Format the hotkey for display
	const hotkeyDisplay = $derived(formatHotkeyConfig($hotkeyPreferences.globalSttHotkey));

	// Auto-dismiss errors after 5 seconds
	$effect(() => {
		if ($globalSttState.error) {
			const timer = setTimeout(() => {
				clearError();
			}, 5000);
			return () => clearTimeout(timer);
		}
	});
</script>

{#if isVisible}
	<div
		class="global-stt-indicator"
		class:recording={$globalSttState.isRecording}
		class:transcribing={$globalSttState.isTranscribing}
		class:preparing={$globalSttState.isPreparing}
		class:error={$globalSttState.error !== null}
		role="status"
		aria-live="polite"
	>
		<div class="indicator-content">
			<!-- Icon Area -->
			<div class="indicator-icon">
				{#if $globalSttState.error}
					<!-- Error icon -->
					<svg class="icon error-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<circle cx="12" cy="12" r="10" />
						<line x1="15" y1="9" x2="9" y2="15" />
						<line x1="9" y1="9" x2="15" y2="15" />
					</svg>
				{:else if $globalSttState.isRecording}
					<!-- Animated microphone with audio rings -->
					<div class="audio-rings">
						<div
							class="ring ring-1"
							style="transform: scale({1 + $globalSttState.audioLevel * 0.5}); opacity: {0.3 + $globalSttState.audioLevel * 0.4}"
						></div>
						<div
							class="ring ring-2"
							style="transform: scale({1 + $globalSttState.audioLevel * 0.8}); opacity: {0.2 + $globalSttState.audioLevel * 0.3}"
						></div>
					</div>
					<svg class="icon mic-icon" viewBox="0 0 24 24" fill="currentColor">
						<path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z" />
						<path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z" />
					</svg>
				{:else if $globalSttState.isTranscribing}
					<!-- Processing animation -->
					<div class="processing-dots">
						<span class="dot"></span>
						<span class="dot"></span>
						<span class="dot"></span>
					</div>
				{:else if $globalSttState.isPreparing}
					<!-- Preparing spinner -->
					<div class="preparing-spinner"></div>
				{/if}
			</div>

			<!-- Text Area -->
			<div class="indicator-text">
				{#if $globalSttState.error}
					<span class="status-text error-text">{$globalSttState.error}</span>
					<button class="dismiss-btn" onclick={() => clearError()} aria-label="Dismiss error">
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<line x1="18" y1="6" x2="6" y2="18" />
							<line x1="6" y1="6" x2="18" y2="18" />
						</svg>
					</button>
				{:else if $globalSttState.isRecording}
					<span class="status-text">Listening...</span>
					<kbd class="hotkey-hint">{hotkeyDisplay}</kbd>
					<span class="hint-text">to stop</span>
				{:else if $globalSttState.isTranscribing}
					<span class="status-text">Transcribing...</span>
				{:else if $globalSttState.isPreparing}
					<span class="status-text">Starting microphone...</span>
				{/if}
			</div>
		</div>
	</div>
{/if}

<style>
	.global-stt-indicator {
		position: fixed;
		bottom: 16px;
		left: 50%;
		transform: translateX(-50%);
		z-index: 9999;

		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: 10px;
		box-shadow:
			0 4px 6px -1px rgba(0, 0, 0, 0.1),
			0 2px 4px -1px rgba(0, 0, 0, 0.06),
			0 0 0 1px rgba(0, 0, 0, 0.05);
		backdrop-filter: blur(20px);

		animation: slide-up 0.2s ease-out;
	}

	.global-stt-indicator.recording {
		border-color: var(--destructive);
		box-shadow:
			0 0 20px rgba(239, 68, 68, 0.2),
			0 4px 6px -1px rgba(0, 0, 0, 0.1);
	}

	.global-stt-indicator.transcribing {
		border-color: var(--primary);
	}

	.global-stt-indicator.error {
		border-color: var(--destructive);
		background: var(--destructive-15, rgba(239, 68, 68, 0.1));
	}

	.indicator-content {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 12px;
	}

	.indicator-icon {
		position: relative;
		width: 32px;
		height: 32px;
		display: flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
	}

	.icon {
		width: 20px;
		height: 20px;
		z-index: 1;
	}

	.mic-icon {
		color: var(--destructive);
	}

	.error-icon {
		color: var(--destructive);
	}

	.audio-rings {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.ring {
		position: absolute;
		width: 26px;
		height: 26px;
		border: 2px solid var(--destructive);
		border-radius: 50%;
		transition: transform 0.1s ease-out, opacity 0.1s ease-out;
	}

	.ring-1 {
		opacity: 0.3;
	}

	.ring-2 {
		opacity: 0.2;
	}

	.processing-dots {
		display: flex;
		gap: 4px;
	}

	.dot {
		width: 6px;
		height: 6px;
		background: var(--primary);
		border-radius: 50%;
		animation: bounce 1.2s infinite ease-in-out;
	}

	.dot:nth-child(1) {
		animation-delay: 0s;
	}

	.dot:nth-child(2) {
		animation-delay: 0.2s;
	}

	.dot:nth-child(3) {
		animation-delay: 0.4s;
	}

	.preparing-spinner {
		width: 20px;
		height: 20px;
		border: 2px solid var(--muted);
		border-top-color: var(--primary);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}

	.indicator-text {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}

	.status-text {
		font-size: 13px;
		font-weight: 500;
		color: var(--foreground);
	}

	.error-text {
		color: var(--destructive);
		max-width: 300px;
	}

	.hotkey-hint {
		display: inline-flex;
		align-items: center;
		padding: 1px 5px;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: 3px;
		font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Monaco, monospace;
		font-size: 10px;
		font-weight: 600;
		color: var(--muted-foreground);
	}

	.hint-text {
		font-size: 12px;
		color: var(--muted-foreground);
	}

	.dismiss-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		height: 18px;
		padding: 0;
		background: transparent;
		border: none;
		border-radius: 3px;
		cursor: pointer;
		color: var(--muted-foreground);
		transition: color 0.15s, background 0.15s;
	}

	.dismiss-btn:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	.dismiss-btn svg {
		width: 12px;
		height: 12px;
	}

	@keyframes slide-up {
		from {
			opacity: 0;
			transform: translateX(-50%) translateY(20px);
		}
		to {
			opacity: 1;
			transform: translateX(-50%) translateY(0);
		}
	}

	@keyframes bounce {
		0%,
		80%,
		100% {
			transform: scale(0.8);
			opacity: 0.5;
		}
		40% {
			transform: scale(1.2);
			opacity: 1;
		}
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	/* Dark mode adjustments */
	:global(.dark) .global-stt-indicator {
		box-shadow:
			0 4px 6px -1px rgba(0, 0, 0, 0.3),
			0 2px 4px -1px rgba(0, 0, 0, 0.2),
			0 0 0 1px rgba(255, 255, 255, 0.05);
	}

	:global(.dark) .global-stt-indicator.recording {
		box-shadow:
			0 0 30px rgba(239, 68, 68, 0.3),
			0 4px 6px -1px rgba(0, 0, 0, 0.3);
	}
</style>
