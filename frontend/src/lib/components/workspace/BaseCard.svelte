<script lang="ts">
	/**
	 * BaseCard - Card chrome with title bar and window controls
	 *
	 * This component provides:
	 * - Title bar with drag handle
	 * - Minimize/maximize/close buttons
	 * - Content slot for card-specific content
	 *
	 * Card content is rendered based on card type.
	 */

	import type { CardType } from '$lib/stores/workspace';
	import ChatCardContent from './cards/ChatCardContent.svelte';

	interface Props {
		title: string;
		cardType: CardType;
		isFocused?: boolean;
		isMaximized?: boolean;
		onDragStart: (e: MouseEvent) => void;
		onMinimize: () => void;
		onMaximize: () => void;
		onClose: () => void;
		cardId: string;
		dataId: string | null;
	}

	let {
		title,
		cardType,
		isFocused = false,
		isMaximized = false,
		onDragStart,
		onMinimize,
		onMaximize,
		onClose,
		cardId,
		dataId
	}: Props = $props();

	function handleDoubleClick() {
		onMaximize();
	}
</script>

<div class="base-card" class:focused={isFocused}>
	<!-- Title bar -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="title-bar"
		onmousedown={onDragStart}
		ondblclick={handleDoubleClick}
	>
		<div class="title-bar-left">
			<!-- Window controls (macOS style) -->
			<div class="window-controls">
				<button
					class="control close"
					onclick={(e) => { e.stopPropagation(); onClose(); }}
					title="Close"
				>
					<svg viewBox="0 0 12 12">
						<path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" stroke-width="1.5" />
					</svg>
				</button>
				<button
					class="control minimize"
					onclick={(e) => { e.stopPropagation(); onMinimize(); }}
					title="Minimize"
				>
					<svg viewBox="0 0 12 12">
						<path d="M2 6h8" stroke="currentColor" stroke-width="1.5" />
					</svg>
				</button>
				<button
					class="control maximize"
					onclick={(e) => { e.stopPropagation(); onMaximize(); }}
					title={isMaximized ? 'Restore' : 'Maximize'}
				>
					<svg viewBox="0 0 12 12">
						{#if isMaximized}
							<path d="M3 5h4v4M9 7V3H5" stroke="currentColor" stroke-width="1.2" />
						{:else}
							<rect x="2" y="2" width="8" height="8" rx="1" stroke="currentColor" stroke-width="1.2" fill="none" />
						{/if}
					</svg>
				</button>
			</div>
		</div>

		<div class="title-text">{title}</div>

		<div class="title-bar-right">
			<!-- Reserved for future card actions -->
		</div>
	</div>

	<!-- Card content -->
	<div class="card-content">
		{#if cardType === 'chat'}
			<ChatCardContent {cardId} sessionId={dataId} />
		{:else}
			<div class="placeholder-content">
				<p>Card type: {cardType}</p>
				<p>Coming soon...</p>
			</div>
		{/if}
	</div>
</div>

<style>
	.base-card {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: #1a1a1a;
	}

	.title-bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		height: 38px;
		padding: 0 12px;
		background: linear-gradient(180deg, #2a2a2a 0%, #222 100%);
		border-bottom: 1px solid rgba(0, 0, 0, 0.3);
		cursor: grab;
		user-select: none;
		flex-shrink: 0;
	}

	.title-bar:active {
		cursor: grabbing;
	}

	.title-bar-left,
	.title-bar-right {
		flex: 1;
		display: flex;
		align-items: center;
	}

	.title-bar-right {
		justify-content: flex-end;
	}

	.window-controls {
		display: flex;
		gap: 8px;
	}

	.control {
		width: 14px;
		height: 14px;
		border-radius: 50%;
		border: none;
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 0;
		transition: opacity 0.15s ease;
	}

	.control svg {
		width: 8px;
		height: 8px;
		opacity: 0;
		transition: opacity 0.15s ease;
	}

	.window-controls:hover .control svg {
		opacity: 1;
	}

	.control.close {
		background: #ff5f57;
		color: #4a0002;
	}

	.control.close:hover {
		background: #ff3b30;
	}

	.control.minimize {
		background: #febc2e;
		color: #5a4500;
	}

	.control.minimize:hover {
		background: #ffcc00;
	}

	.control.maximize {
		background: #28c840;
		color: #0a3a10;
	}

	.control.maximize:hover {
		background: #30d158;
	}

	.title-text {
		font-size: 0.8125rem;
		font-weight: 500;
		color: rgba(255, 255, 255, 0.8);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 50%;
		text-align: center;
	}

	.focused .title-text {
		color: white;
	}

	.card-content {
		flex: 1;
		overflow: hidden;
		display: flex;
		flex-direction: column;
	}

	.placeholder-content {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		color: rgba(255, 255, 255, 0.4);
		font-size: 0.875rem;
	}
</style>
