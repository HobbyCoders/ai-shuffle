<script lang="ts">
	/**
	 * FileBrowserCard - File browser card for The Deck
	 *
	 * Wraps the existing FilesView component in a BaseCard for the deck UI.
	 * Provides full file management: browse, upload, download, rename, delete.
	 */

	import BaseCard from './BaseCard.svelte';
	import { FilesView } from '../files';
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
	<!-- Mobile: Full-screen without BaseCard wrapper -->
	<div class="files-card-content mobile">
		<FilesView />
	</div>
{:else}
	<!-- Desktop: Full BaseCard with FilesView inside -->
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		<div class="files-card-content">
			<FilesView />
		</div>
	</BaseCard>
{/if}

<style>
	.files-card-content {
		flex: 1;
		display: flex;
		flex-direction: column;
		overflow: hidden;
		min-height: 0;
		border-radius: 0 0 12px 12px;
	}

	.files-card-content.mobile {
		height: 100%;
		border-radius: 0;
	}

	/* Ensure FilesView fills the card */
	.files-card-content :global(.files-view) {
		flex: 1;
		min-height: 0;
	}
</style>
