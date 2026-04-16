<script lang="ts">
	/**
	 * VoiceSessionCard - Card for the new session creation flow
	 *
	 * Standalone card component that can be placed anywhere to offer
	 * voice session creation. Pairs with a chat card for type selection.
	 */
	import { Mic, AudioWaveform } from 'lucide-svelte';

	interface Props {
		onSelect: () => void;
		compact?: boolean;
	}

	let { onSelect, compact = false }: Props = $props();

	let isHovered = $state(false);
</script>

<button
	class="voice-card"
	class:voice-card-compact={compact}
	onclick={onSelect}
	onmouseenter={() => isHovered = true}
	onmouseleave={() => isHovered = false}
>
	<div class="voice-card-visual">
		<div class="voice-card-icon">
			<Mic size={compact ? 24 : 32} strokeWidth={1.5} />
		</div>
		{#if isHovered && !compact}
			<div class="voice-card-waveform" aria-hidden="true">
				<span class="wave-line"></span>
				<span class="wave-line"></span>
				<span class="wave-line"></span>
				<span class="wave-line"></span>
				<span class="wave-line"></span>
			</div>
		{/if}
	</div>

	<div class="voice-card-text">
		<h3 class="voice-card-title">Voice Session</h3>
		{#if !compact}
			<p class="voice-card-desc">Talk to Claude in real-time with your voice</p>
		{/if}
	</div>

	<div class="voice-card-badge">
		<AudioWaveform size={12} strokeWidth={2} />
		<span>Live</span>
	</div>
</button>

<style>
	.voice-card {
		position: relative;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 1rem;
		padding: 1.75rem 1.25rem;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 0.875rem;
		cursor: pointer;
		transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
		text-align: center;
		overflow: hidden;
	}

	.voice-card-compact {
		flex-direction: row;
		padding: 0.875rem 1rem;
		gap: 0.75rem;
		text-align: left;
	}

	.voice-card:hover {
		border-color: oklch(0.65 0.2 25);
		background: color-mix(in oklch, oklch(0.65 0.2 25) 4%, var(--card));
		box-shadow:
			0 0 0 1px color-mix(in oklch, oklch(0.65 0.2 25) 20%, transparent),
			0 8px 24px color-mix(in oklch, oklch(0.65 0.2 25) 8%, transparent);
		transform: translateY(-2px);
	}

	.voice-card:focus-visible {
		outline: 2px solid oklch(0.65 0.2 25);
		outline-offset: 2px;
	}

	.voice-card:active {
		transform: translateY(0);
	}

	.voice-card-compact:hover {
		transform: translateY(-1px);
	}

	/* Visual area */
	.voice-card-visual {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.voice-card-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 72px;
		height: 72px;
		border-radius: 50%;
		background: color-mix(in oklch, oklch(0.65 0.2 25) 10%, transparent);
		color: oklch(0.65 0.2 25);
		transition: all 0.25s ease;
	}

	.voice-card-compact .voice-card-icon {
		width: 44px;
		height: 44px;
	}

	.voice-card:hover .voice-card-icon {
		background: color-mix(in oklch, oklch(0.65 0.2 25) 18%, transparent);
		box-shadow: 0 0 24px color-mix(in oklch, oklch(0.65 0.2 25) 15%, transparent);
	}

	/* Animated waveform on hover */
	.voice-card-waveform {
		position: absolute;
		bottom: -8px;
		display: flex;
		gap: 3px;
		align-items: flex-end;
		height: 16px;
	}

	.wave-line {
		width: 3px;
		border-radius: 1.5px;
		background: oklch(0.65 0.2 25);
		animation: wave-bounce 0.8s ease-in-out infinite;
	}

	.wave-line:nth-child(1) { height: 6px; animation-delay: 0s; }
	.wave-line:nth-child(2) { height: 12px; animation-delay: 0.1s; }
	.wave-line:nth-child(3) { height: 8px; animation-delay: 0.2s; }
	.wave-line:nth-child(4) { height: 14px; animation-delay: 0.15s; }
	.wave-line:nth-child(5) { height: 5px; animation-delay: 0.25s; }

	@keyframes wave-bounce {
		0%, 100% { transform: scaleY(1); }
		50% { transform: scaleY(0.4); }
	}

	/* Text */
	.voice-card-text {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.voice-card-compact .voice-card-text {
		flex: 1;
	}

	.voice-card-title {
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0;
	}

	.voice-card-desc {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		margin: 0;
		line-height: 1.4;
	}

	/* Live badge */
	.voice-card-badge {
		position: absolute;
		top: 0.625rem;
		right: 0.625rem;
		display: flex;
		align-items: center;
		gap: 0.25rem;
		padding: 0.1875rem 0.5rem;
		font-size: 0.625rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: oklch(0.65 0.2 25);
		background: color-mix(in oklch, oklch(0.65 0.2 25) 10%, transparent);
		border: 1px solid color-mix(in oklch, oklch(0.65 0.2 25) 25%, transparent);
		border-radius: 9999px;
	}

	.voice-card-compact .voice-card-badge {
		position: static;
		flex-shrink: 0;
	}

	@media (prefers-reduced-motion: reduce) {
		.wave-line {
			animation: none;
		}
		.voice-card {
			transition: none;
		}
	}
</style>
