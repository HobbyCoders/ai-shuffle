<script lang="ts">
	/**
	 * ActivityOverlay - Slide-up overlay container for settings panels
	 *
	 * Slides up from the bottom of the Activity Panel to show context-specific
	 * settings like chat configuration, agent setup, etc.
	 */

	import { X } from 'lucide-svelte';
	import { fade, fly } from 'svelte/transition';
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

	function handleBackdropClick(e: MouseEvent) {
		if (e.target === e.currentTarget) {
			onClose?.();
		}
	}

	function handleKeyDown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			onClose?.();
		}
	}
</script>

<svelte:window onkeydown={handleKeyDown} />

{#if type}
	<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
	<div
		class="overlay-backdrop"
		onclick={handleBackdropClick}
		transition:fade={{ duration: 150 }}
	>
		<div
			class="overlay-panel"
			transition:fly={{ y: 200, duration: 200 }}
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
	</div>
{/if}

<style>
	.overlay-backdrop {
		position: absolute;
		inset: 0;
		background: rgba(0, 0, 0, 0.4);
		z-index: 100;
		display: flex;
		flex-direction: column;
		justify-content: flex-end;
	}

	.overlay-panel {
		background: var(--card);
		border-top: 1px solid var(--border);
		border-radius: 12px 12px 0 0;
		max-height: 85%;
		display: flex;
		flex-direction: column;
		box-shadow: 0 -4px 24px rgba(0, 0, 0, 0.15);
	}

	.overlay-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 12px 16px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
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
