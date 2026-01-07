<script lang="ts">
	/**
	 * ModelStudioCard - 3D model generation studio card for The Deck
	 *
	 * Provides interface for AI 3D model generation and viewing.
	 * Supports both desktop (with BaseCard wrapper) and mobile (standalone) modes.
	 */

	import { Box, RotateCcw, Layers, Download, Upload, Sparkles } from 'lucide-svelte';
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
				<Box size={48} strokeWidth={1.5} />
			</div>
			<h3 class="placeholder-title">3D Models</h3>
			<p class="placeholder-text">AI 3D model generation & viewing</p>
		</div>
		<div class="studio-preview">
			<div class="preview-grid">
				<div class="preview-item">
					<Sparkles size={24} />
					<span>Generate</span>
				</div>
				<div class="preview-item">
					<RotateCcw size={24} />
					<span>Rotate</span>
				</div>
				<div class="preview-item">
					<Layers size={24} />
					<span>Layers</span>
				</div>
				<div class="preview-item">
					<Download size={24} />
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
					<Box size={48} strokeWidth={1.5} />
				</div>
				<h3 class="placeholder-title">3D Models</h3>
				<p class="placeholder-text">AI 3D model generation & viewing</p>
			</div>
			<div class="studio-preview">
				<div class="preview-grid">
					<div class="preview-item">
						<Sparkles size={24} />
						<span>Generate</span>
					</div>
					<div class="preview-item">
						<RotateCcw size={24} />
						<span>Rotate</span>
					</div>
					<div class="preview-item">
						<Layers size={24} />
						<span>Layers</span>
					</div>
					<div class="preview-item">
						<Download size={24} />
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
		background: linear-gradient(180deg, oklch(0.12 0.02 200) 0%, oklch(0.08 0.015 200) 100%);
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
		background: linear-gradient(135deg, oklch(0.35 0.12 200) 0%, oklch(0.25 0.1 180) 100%);
		border-radius: 20px;
		color: white;
		margin-bottom: 8px;
		box-shadow:
			0 4px 20px oklch(0.35 0.12 200 / 0.3),
			inset 0 1px 0 rgba(255, 255, 255, 0.2);
		/* 3D effect with perspective */
		transform: perspective(500px) rotateX(5deg) rotateY(-5deg);
	}

	.placeholder-title {
		font-size: 1.25rem;
		font-weight: 600;
		color: oklch(0.9 0.02 200);
		margin: 0;
	}

	.placeholder-text {
		font-size: 0.875rem;
		color: oklch(0.6 0.02 200);
		margin: 0;
	}

	.studio-preview {
		background: oklch(0.06 0.015 200);
		border-radius: 12px;
		padding: 20px;
		margin-top: auto;
		border: 1px solid oklch(0.18 0.02 200);
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
		gap: 8px;
		padding: 16px 12px;
		background: oklch(0.12 0.02 200);
		border-radius: 10px;
		color: oklch(0.7 0.03 200);
		font-size: 0.75rem;
		font-weight: 500;
		transition: all 0.2s ease;
		cursor: pointer;
		border: 1px solid transparent;
	}

	.preview-item:hover {
		background: oklch(0.18 0.03 200);
		color: oklch(0.9 0.03 200);
		border-color: oklch(0.35 0.1 200);
		transform: translateY(-2px);
	}

	.preview-item :global(svg) {
		opacity: 0.8;
	}

	.preview-item:hover :global(svg) {
		opacity: 1;
	}
</style>
