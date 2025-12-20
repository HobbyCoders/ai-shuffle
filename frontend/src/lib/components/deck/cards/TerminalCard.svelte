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
		background: hsl(var(--background));
		color: var(--success);
		font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
		font-size: 0.875rem;
		padding: 16px;
		overflow: auto;
		min-height: 100px;
		border-radius: 0 0 12px 12px;
	}

	.terminal-line {
		line-height: 1.5;
		min-height: 1.5em;
	}

	.terminal-prompt {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-top: 8px;
	}

	.prompt-symbol {
		color: #00ff00;
	}

	.cursor {
		width: 8px;
		height: 16px;
		background: #00ff00;
		animation: blink 1s step-end infinite;
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
</style>
