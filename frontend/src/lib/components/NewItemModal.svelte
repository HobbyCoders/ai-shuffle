<script lang="ts">
	/**
	 * NewItemModal - Popup for creating new items (chat or agent)
	 *
	 * Triggered by the plus button in the activity rail.
	 * Provides a clean interface to choose between starting a new chat or agent.
	 */
	import { MessageSquare, Bot, X } from 'lucide-svelte';
	import { createEventDispatcher } from 'svelte';
	import { fly, fade } from 'svelte/transition';

	interface Props {
		show: boolean;
	}

	let { show = $bindable(false) }: Props = $props();

	const dispatch = createEventDispatcher<{
		close: void;
		newChat: void;
		newAgent: void;
	}>();

	function handleClose() {
		show = false;
		dispatch('close');
	}

	function handleNewChat() {
		show = false;
		dispatch('newChat');
	}

	function handleNewAgent() {
		show = false;
		dispatch('newAgent');
	}

	function handleBackdropClick(e: MouseEvent) {
		if (e.target === e.currentTarget) {
			handleClose();
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			handleClose();
		}
	}
</script>

<svelte:window on:keydown={handleKeydown} />

{#if show}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="modal-backdrop"
		onclick={handleBackdropClick}
		transition:fade={{ duration: 150 }}
	>
		<div
			class="modal-content"
			transition:fly={{ y: -20, duration: 200 }}
		>
			<!-- Header -->
			<div class="modal-header">
				<h2 class="modal-title">Create New</h2>
				<button class="close-btn" onclick={handleClose}>
					<X size={18} />
				</button>
			</div>

			<!-- Options -->
			<div class="options">
				<button class="option-btn" onclick={handleNewChat}>
					<div class="option-icon chat-icon">
						<MessageSquare size={24} strokeWidth={1.5} />
					</div>
					<div class="option-text">
						<span class="option-title">New Chat</span>
						<span class="option-desc">Start a conversation with AI</span>
					</div>
				</button>

				<button class="option-btn" onclick={handleNewAgent}>
					<div class="option-icon agent-icon">
						<Bot size={24} strokeWidth={1.5} />
					</div>
					<div class="option-text">
						<span class="option-title">New Agent</span>
						<span class="option-desc">Launch an autonomous agent</span>
					</div>
				</button>
			</div>

			<!-- Keyboard hints -->
			<div class="hints">
				<span class="hint"><kbd>Esc</kbd> to close</span>
			</div>
		</div>
	</div>
{/if}

<style>
	.modal-backdrop {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.5);
		backdrop-filter: blur(4px);
		display: flex;
		align-items: flex-start;
		justify-content: center;
		padding-top: 15vh;
		z-index: 1000;
	}

	.modal-content {
		background: hsl(var(--card));
		border: 1px solid hsl(var(--border));
		border-radius: 16px;
		box-shadow:
			0 25px 50px -12px rgba(0, 0, 0, 0.4),
			0 0 0 1px rgba(255, 255, 255, 0.05);
		min-width: 320px;
		max-width: 400px;
		overflow: hidden;
	}

	.modal-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 16px 20px;
		border-bottom: 1px solid hsl(var(--border) / 0.5);
	}

	.modal-title {
		font-size: 1rem;
		font-weight: 600;
		color: hsl(var(--foreground));
		margin: 0;
	}

	.close-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: hsl(var(--muted-foreground));
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.close-btn:hover {
		background: hsl(var(--muted));
		color: hsl(var(--foreground));
	}

	.options {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 12px;
	}

	.option-btn {
		display: flex;
		align-items: center;
		gap: 14px;
		width: 100%;
		padding: 14px 16px;
		background: transparent;
		border: 1px solid transparent;
		border-radius: 12px;
		cursor: pointer;
		transition: all 0.2s ease;
		text-align: left;
	}

	.option-btn:hover {
		background: hsl(var(--accent));
		border-color: hsl(var(--border));
	}

	.option-btn:active {
		transform: scale(0.98);
	}

	.option-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 48px;
		height: 48px;
		border-radius: 12px;
		flex-shrink: 0;
	}

	.chat-icon {
		background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%);
		color: white;
		box-shadow: 0 4px 12px -2px rgba(59, 130, 246, 0.4);
	}

	.agent-icon {
		background: linear-gradient(135deg, #10b981 0%, #059669 100%);
		color: white;
		box-shadow: 0 4px 12px -2px rgba(16, 185, 129, 0.4);
	}

	.option-text {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.option-title {
		font-size: 0.9375rem;
		font-weight: 600;
		color: hsl(var(--foreground));
	}

	.option-desc {
		font-size: 0.8125rem;
		color: hsl(var(--muted-foreground));
	}

	.hints {
		display: flex;
		justify-content: center;
		padding: 12px 16px;
		border-top: 1px solid hsl(var(--border) / 0.5);
	}

	.hint {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
	}

	kbd {
		display: inline-block;
		padding: 2px 6px;
		background: hsl(var(--muted));
		border: 1px solid hsl(var(--border));
		border-radius: 4px;
		font-family: inherit;
		font-size: 0.6875rem;
		font-weight: 500;
	}
</style>
