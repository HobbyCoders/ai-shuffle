<script lang="ts">
	/**
	 * CardDock - Tray for minimized cards
	 *
	 * Appears at the bottom of the workspace when cards are minimized.
	 * Click to restore a card.
	 */

	import { workspace, type WorkspaceCard } from '$lib/stores/workspace';

	interface Props {
		cards: WorkspaceCard[];
	}

	let { cards }: Props = $props();

	function restoreCard(cardId: string) {
		workspace.restoreCard(cardId);
	}

	function getCardIcon(type: string): string {
		switch (type) {
			case 'chat':
				return 'M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z';
			case 'settings':
				return 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z M15 12a3 3 0 11-6 0 3 3 0 016 0z';
			default:
				return 'M4 5a1 1 0 011-1h14a1 1 0 011 1v2a1 1 0 01-1 1H5a1 1 0 01-1-1V5zM4 13a1 1 0 011-1h6a1 1 0 011 1v6a1 1 0 01-1 1H5a1 1 0 01-1-1v-6zM16 13a1 1 0 011-1h2a1 1 0 011 1v6a1 1 0 01-1 1h-2a1 1 0 01-1-1v-6z';
		}
	}
</script>

<div class="card-dock">
	{#each cards as card (card.id)}
		<button
			class="dock-item"
			onclick={() => restoreCard(card.id)}
			title={card.title}
		>
			<svg
				class="dock-icon"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="1.5"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d={getCardIcon(card.type)} />
			</svg>
			<span class="dock-label">{card.title}</span>
		</button>
	{/each}
</div>

<style>
	.card-dock {
		position: absolute;
		bottom: 16px;
		left: 50%;
		transform: translateX(-50%);
		display: flex;
		gap: 8px;
		padding: 8px 12px;
		background: rgba(30, 30, 30, 0.95);
		border-radius: 12px;
		border: 1px solid rgba(255, 255, 255, 0.1);
		box-shadow:
			0 4px 6px -1px rgba(0, 0, 0, 0.3),
			0 2px 4px -2px rgba(0, 0, 0, 0.2);
		backdrop-filter: blur(12px);
		z-index: 1000;
	}

	.dock-item {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 6px 12px;
		background: rgba(255, 255, 255, 0.05);
		border: 1px solid rgba(255, 255, 255, 0.1);
		border-radius: 8px;
		color: rgba(255, 255, 255, 0.8);
		font-size: 0.8125rem;
		cursor: pointer;
		transition: all 0.15s ease;
		max-width: 150px;
	}

	.dock-item:hover {
		background: rgba(255, 255, 255, 0.1);
		border-color: rgba(255, 165, 0, 0.3);
		color: white;
	}

	.dock-icon {
		width: 16px;
		height: 16px;
		flex-shrink: 0;
	}

	.dock-label {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
</style>
