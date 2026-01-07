<script lang="ts">
	/**
	 * TerminalCard - Terminal card placeholder for The Deck
	 *
	 * Terminal integration coming in a future phase.
	 */

	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMinimize: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
	}

	let { card, onClose, onMinimize, onMaximize, onFocus, onMove, onResize, onDragEnd, onResizeEnd }: Props = $props();
</script>

<BaseCard {card} {onClose} {onMinimize} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
	<div class="terminal-content">
		<div class="terminal-line">Terminal integration coming soon...</div>
		<div class="terminal-line"></div>
		<div class="terminal-prompt">
			<span class="prompt-symbol">$</span>
			<span class="cursor"></span>
		</div>
	</div>
</BaseCard>

<style>
	.terminal-content {
		flex: 1;
		display: flex;
		flex-direction: column;
		background: oklch(0.08 0.01 260);
		color: var(--foreground);
		font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
		font-size: 0.875rem;
		padding: 16px;
		overflow: auto;
		min-height: 100px;
		border-radius: 0 0 12px 12px;
		box-shadow: inset 0 2px 8px oklch(0 0 0 / 0.3);
	}

	/* Light mode terminal - still dark for authentic look but slightly lighter */
	:global(.light) .terminal-content {
		background: oklch(0.15 0.01 260);
		box-shadow: inset 0 2px 8px oklch(0 0 0 / 0.2);
	}

	.terminal-line {
		line-height: 1.6;
		min-height: 1.6em;
		color: oklch(0.85 0.02 260);
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
