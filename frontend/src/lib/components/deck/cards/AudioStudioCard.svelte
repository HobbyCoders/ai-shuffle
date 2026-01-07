<script lang="ts">
	/**
	 * AudioStudioCard - Audio generation studio card for The Deck
	 *
	 * Provides interface for AI audio generation, music, and sound effects.
	 * Supports both desktop (with BaseCard wrapper) and mobile (standalone) modes.
	 */

	import { AudioLines, Mic, Music, Volume2, Download, Play } from 'lucide-svelte';
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		mobile?: boolean;
	}

	let { card, onClose, onMaximize, onFocus, onMove, onResize, onDragEnd, onResizeEnd, mobile = false }: Props = $props();
</script>

{#if mobile}
	<!-- Mobile: Full-screen card with no BaseCard wrapper -->
	<div class="studio-content mobile">
		<div class="studio-placeholder">
			<div class="placeholder-icon">
				<AudioLines size={48} strokeWidth={1.5} />
			</div>
			<h3 class="placeholder-title">Audio Studio</h3>
			<p class="placeholder-text">AI music & sound generation</p>
		</div>
		<div class="studio-preview">
			<div class="waveform">
				{#each Array(20) as _, i}
					<div class="wave-bar" style="--delay: {i * 0.05}s; --height: {30 + Math.sin(i * 0.5) * 20}%"></div>
				{/each}
			</div>
			<div class="preview-grid">
				<div class="preview-item">
					<Music size={20} />
					<span>Music</span>
				</div>
				<div class="preview-item">
					<Volume2 size={20} />
					<span>Effects</span>
				</div>
				<div class="preview-item">
					<Mic size={20} />
					<span>Voice</span>
				</div>
				<div class="preview-item">
					<Download size={20} />
					<span>Export</span>
				</div>
			</div>
		</div>
	</div>
{:else}
	<!-- Desktop: Full BaseCard with all features -->
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		<div class="studio-content">
			<div class="studio-placeholder">
				<div class="placeholder-icon">
					<AudioLines size={48} strokeWidth={1.5} />
				</div>
				<h3 class="placeholder-title">Audio Studio</h3>
				<p class="placeholder-text">AI music & sound generation</p>
			</div>
			<div class="studio-preview">
				<div class="waveform">
					{#each Array(20) as _, i}
						<div class="wave-bar" style="--delay: {i * 0.05}s; --height: {30 + Math.sin(i * 0.5) * 20}%"></div>
					{/each}
				</div>
				<div class="preview-grid">
					<div class="preview-item">
						<Music size={20} />
						<span>Music</span>
					</div>
					<div class="preview-item">
						<Volume2 size={20} />
						<span>Effects</span>
					</div>
					<div class="preview-item">
						<Mic size={20} />
						<span>Voice</span>
					</div>
					<div class="preview-item">
						<Download size={20} />
						<span>Export</span>
					</div>
				</div>
			</div>
		</div>
	</BaseCard>
{/if}

<style>
	.studio-content {
		flex: 1;
		display: flex;
		flex-direction: column;
		background: linear-gradient(180deg, oklch(0.12 0.03 25) 0%, oklch(0.08 0.02 25) 100%);
		color: var(--foreground);
		padding: 24px;
		overflow: auto;
		min-height: 100px;
		border-radius: 0 0 12px 12px;
	}

	.studio-content.mobile {
		height: 100%;
		border-radius: 0;
	}

	.studio-placeholder {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 12px;
		text-align: center;
		padding: 24px;
	}

	.placeholder-icon {
		width: 80px;
		height: 80px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: linear-gradient(135deg, oklch(0.5 0.18 25) 0%, oklch(0.4 0.15 10) 100%);
		border-radius: 20px;
		color: white;
		margin-bottom: 8px;
		box-shadow:
			0 4px 20px oklch(0.5 0.18 25 / 0.3),
			inset 0 1px 0 rgba(255, 255, 255, 0.2);
	}

	.placeholder-title {
		font-size: 1.25rem;
		font-weight: 600;
		color: oklch(0.9 0.02 25);
		margin: 0;
	}

	.placeholder-text {
		font-size: 0.875rem;
		color: oklch(0.6 0.02 25);
		margin: 0;
	}

	.studio-preview {
		background: oklch(0.06 0.02 25);
		border-radius: 12px;
		padding: 20px;
		margin-top: auto;
		border: 1px solid oklch(0.18 0.03 25);
	}

	.waveform {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 3px;
		height: 60px;
		margin-bottom: 16px;
		padding: 0 10px;
	}

	.wave-bar {
		width: 4px;
		height: var(--height, 50%);
		background: linear-gradient(180deg, oklch(0.6 0.15 25) 0%, oklch(0.4 0.12 25) 100%);
		border-radius: 2px;
		animation: pulse 1.5s ease-in-out infinite;
		animation-delay: var(--delay, 0s);
	}

	@keyframes pulse {
		0%, 100% {
			transform: scaleY(1);
			opacity: 0.7;
		}
		50% {
			transform: scaleY(1.3);
			opacity: 1;
		}
	}

	.preview-grid {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 12px;
	}

	.preview-item {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 6px;
		padding: 12px 10px;
		background: oklch(0.12 0.02 25);
		border-radius: 10px;
		color: oklch(0.7 0.03 25);
		font-size: 0.7rem;
		font-weight: 500;
		transition: all 0.2s ease;
		cursor: pointer;
		border: 1px solid transparent;
	}

	.preview-item:hover {
		background: oklch(0.18 0.03 25);
		color: oklch(0.9 0.03 25);
		border-color: oklch(0.5 0.12 25);
		transform: translateY(-2px);
	}

	.preview-item :global(svg) {
		opacity: 0.8;
	}

	.preview-item:hover :global(svg) {
		opacity: 1;
	}
</style>
