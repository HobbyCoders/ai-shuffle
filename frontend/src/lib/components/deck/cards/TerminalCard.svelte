<script lang="ts">
	/**
	 * TerminalCard - Terminal card for The Deck
	 *
	 * Currently shows a placeholder with "coming soon" message.
	 * Supports both desktop (with BaseCard wrapper) and mobile (standalone) modes.
	 */

	import { Terminal } from 'lucide-svelte';
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
	<div class="terminal-content mobile">
		<div class="terminal-placeholder">
			<div class="placeholder-icon">
				<Terminal size={48} strokeWidth={1.5} />
			</div>
			<h3 class="placeholder-title">Terminal</h3>
			<p class="placeholder-text">Terminal integration coming soon...</p>
		</div>
		<div class="terminal-preview">
			<div class="terminal-line">$ <span class="command">claude</span></div>
			<div class="terminal-line output">Starting Claude Code...</div>
			<div class="terminal-prompt">
				<span class="prompt-symbol">$</span>
				<span class="cursor"></span>
			</div>
		</div>
	</div>
{:else}
	<!-- Desktop: Full BaseCard with all features -->
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		<div class="terminal-content">
			<div class="terminal-placeholder">
				<div class="placeholder-icon">
					<Terminal size={48} strokeWidth={1.5} />
				</div>
				<h3 class="placeholder-title">Terminal</h3>
				<p class="placeholder-text">Terminal integration coming soon...</p>
			</div>
			<div class="terminal-preview">
				<div class="terminal-line">$ <span class="command">claude</span></div>
				<div class="terminal-line output">Starting Claude Code...</div>
				<div class="terminal-prompt">
					<span class="prompt-symbol">$</span>
					<span class="cursor"></span>
				</div>
			</div>
		</div>
	</BaseCard>
{/if}

<style>
	.terminal-content {
		flex: 1;
		display: flex;
		flex-direction: column;
		background: oklch(0.08 0.01 260);
		color: var(--foreground);
		font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
		font-size: 0.875rem;
		padding: 24px;
		overflow: auto;
		min-height: 100px;
		border-radius: 0 0 12px 12px;
		box-shadow: inset 0 2px 8px oklch(0 0 0 / 0.3);
	}

	.terminal-content.mobile {
		height: 100%;
		border-radius: 0;
	}

	/* Light mode terminal - still dark for authentic look but slightly lighter */
	:global(.light) .terminal-content {
		background: oklch(0.15 0.01 260);
		box-shadow: inset 0 2px 8px oklch(0 0 0 / 0.2);
	}

	.terminal-placeholder {
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
		background: oklch(0.15 0.01 260);
		border-radius: 16px;
		color: var(--success);
		margin-bottom: 8px;
	}

	:global(.light) .placeholder-icon {
		background: oklch(0.22 0.01 260);
	}

	.placeholder-title {
		font-size: 1.125rem;
		font-weight: 600;
		color: oklch(0.9 0.02 260);
		margin: 0;
	}

	.placeholder-text {
		font-size: 0.875rem;
		color: oklch(0.6 0.01 260);
		margin: 0;
	}

	.terminal-preview {
		background: oklch(0.06 0.01 260);
		border-radius: 8px;
		padding: 16px;
		margin-top: auto;
		border: 1px solid oklch(0.15 0.01 260);
	}

	:global(.light) .terminal-preview {
		background: oklch(0.12 0.01 260);
		border-color: oklch(0.2 0.01 260);
	}

	.terminal-line {
		line-height: 1.6;
		min-height: 1.6em;
		color: oklch(0.85 0.02 260);
	}

	.terminal-line .command {
		color: var(--primary);
	}

	.terminal-line.output {
		color: oklch(0.6 0.01 260);
	}

	:global(.light) .terminal-line {
		color: oklch(0.90 0.02 260);
	}

	.terminal-prompt {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-top: 8px;
	}

	.prompt-symbol {
		color: var(--success);
		font-weight: 600;
		text-shadow: 0 0 8px var(--success);
	}

	.cursor {
		width: 8px;
		height: 18px;
		background: var(--success);
		border-radius: 1px;
		animation: blink 1s step-end infinite;
		box-shadow: 0 0 6px var(--success);
	}

	@keyframes blink {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0;
		}
	}

	/* Hover effect on the terminal content */
	.terminal-content:hover {
		background: oklch(0.09 0.01 260);
	}

	:global(.light) .terminal-content:hover {
		background: oklch(0.16 0.01 260);
	}
</style>
