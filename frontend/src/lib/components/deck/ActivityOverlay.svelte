<script lang="ts">
	/**
	 * ActivityOverlay - Full-screen overlay for settings panels
	 *
	 * Takes over the entire Activity Panel to show context-specific
	 * settings like chat configuration, agent setup, etc.
	 */

	import { X } from 'lucide-svelte';
	import { fade } from 'svelte/transition';
	import type { OverlayType } from './ActivityPanel.svelte';
	import ChatSettingsOverlay from './overlays/ChatSettingsOverlay.svelte';

	interface Props {
		type?: OverlayType;
		data?: Record<string, unknown>;
		onClose?: () => void;
	}

	let {
		type = null,
		data = {},
		onClose
	}: Props = $props();

	// Type-safe data for overlays
	const chatSettingsData = $derived(data as Record<string, unknown>);

	function handleKeyDown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			onClose?.();
		}
	}
</script>

<svelte:window onkeydown={handleKeyDown} />

{#if type}
	<div
		class="overlay-panel"
		transition:fade={{ duration: 150 }}
	>
		<!-- Overlay Header -->
		<div class="overlay-header">
			<span class="overlay-title">
				{#if type === 'chat-settings'}
					Chat Settings
				{/if}
			</span>
			<button
				type="button"
				class="close-btn"
				onclick={() => onClose?.()}
				aria-label="Close overlay"
			>
				<X size={16} />
			</button>
		</div>

		<!-- Overlay Content -->
		<div class="overlay-content">
			{#if type === 'chat-settings'}
				<ChatSettingsOverlay {...chatSettingsData} onClose={onClose} />
			{/if}
		</div>
	</div>
{/if}

<style>
	.overlay-panel {
		display: flex;
		flex-direction: column;
		height: 100%;
		width: 100%;
		background: var(--card);
	}

	.overlay-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 12px 16px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
		background: var(--card);
	}

	.overlay-title {
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--foreground);
	}

	.close-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		border-radius: 6px;
		background: transparent;
		color: var(--muted-foreground);
		transition: all 0.15s ease;
	}

	.close-btn:hover {
		background: var(--accent);
		color: var(--foreground);
	}

	.overlay-content {
		flex: 1;
		overflow-y: auto;
		min-height: 0;
	}
</style>
