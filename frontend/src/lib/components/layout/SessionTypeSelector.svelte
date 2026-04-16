<script lang="ts">
	/**
	 * SessionTypeSelector - Modal for choosing session type (Chat or Voice)
	 *
	 * Displayed when creating a new session. Two cards side by side.
	 * Clicking a card fires the onSelect callback with the session type.
	 */
	import { MessageSquare, Mic } from 'lucide-svelte';

	interface Props {
		open: boolean;
		onSelect: (type: 'chat' | 'voice') => void;
		onClose: () => void;
	}

	let { open, onSelect, onClose }: Props = $props();

	function handleSelect(type: 'chat' | 'voice') {
		onSelect(type);
	}

	function handleBackdropClick(e: MouseEvent) {
		if (e.target === e.currentTarget) {
			onClose();
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			onClose();
		}
	}
</script>

{#if open}
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<!-- svelte-ignore a11y_interactive_supports_focus -->
	<div
		class="selector-backdrop"
		role="dialog"
		aria-modal="true"
		aria-label="Choose session type"
		onclick={handleBackdropClick}
		onkeydown={handleKeydown}
	>
		<div class="selector-modal">
			<h2 class="selector-heading">New Session</h2>
			<p class="selector-subtext">Choose how you want to interact</p>

			<div class="selector-cards">
				<!-- Chat card -->
				<button
					class="type-card"
					onclick={() => handleSelect('chat')}
				>
					<div class="type-card-icon type-card-icon-chat">
						<MessageSquare size={32} strokeWidth={1.5} />
					</div>
					<h3 class="type-card-title">New Chat</h3>
					<p class="type-card-desc">Text conversation with Claude</p>
				</button>

				<!-- Voice card -->
				<button
					class="type-card"
					onclick={() => handleSelect('voice')}
				>
					<div class="type-card-icon type-card-icon-voice">
						<Mic size={32} strokeWidth={1.5} />
					</div>
					<h3 class="type-card-title">Voice Session</h3>
					<p class="type-card-desc">Talk to Claude in real-time</p>
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.selector-backdrop {
		position: fixed;
		inset: 0;
		z-index: 100;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 1rem;
		background: color-mix(in oklch, var(--background) 70%, transparent);
		backdrop-filter: blur(8px);
		-webkit-backdrop-filter: blur(8px);
		animation: selector-fade-in 0.2s ease-out;
	}

	@keyframes selector-fade-in {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	.selector-modal {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 1rem;
		padding: 2rem;
		max-width: 480px;
		width: 100%;
		box-shadow: var(--shadow-l, 0 8px 32px rgba(0,0,0,0.25));
		animation: selector-slide-up 0.25s cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes selector-slide-up {
		from {
			opacity: 0;
			transform: translateY(12px) scale(0.98);
		}
		to {
			opacity: 1;
			transform: translateY(0) scale(1);
		}
	}

	.selector-heading {
		font-size: 1.25rem;
		font-weight: 700;
		color: var(--foreground);
		margin: 0 0 0.25rem;
		letter-spacing: -0.02em;
	}

	.selector-subtext {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		margin: 0 0 1.5rem;
	}

	.selector-cards {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 0.75rem;
	}

	/* ─── Type Card ─── */

	.type-card {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.75rem;
		padding: 1.5rem 1rem;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 0.75rem;
		cursor: pointer;
		transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
		text-align: center;
	}

	.type-card:hover {
		border-color: var(--primary);
		background: color-mix(in oklch, var(--primary) 5%, var(--background));
		box-shadow: 0 0 0 1px color-mix(in oklch, var(--primary) 30%, transparent),
			0 4px 16px color-mix(in oklch, var(--primary) 10%, transparent);
		transform: translateY(-2px);
	}

	.type-card:focus-visible {
		outline: 2px solid var(--primary);
		outline-offset: 2px;
	}

	.type-card:active {
		transform: translateY(0);
	}

	.type-card-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 64px;
		height: 64px;
		border-radius: 50%;
		transition: all 0.2s ease;
	}

	.type-card-icon-chat {
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
	}

	.type-card:hover .type-card-icon-chat {
		background: color-mix(in oklch, var(--primary) 20%, transparent);
		box-shadow: 0 0 20px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	.type-card-icon-voice {
		background: color-mix(in oklch, oklch(0.65 0.2 25) 12%, transparent);
		color: oklch(0.65 0.2 25);
	}

	.type-card:hover .type-card-icon-voice {
		background: color-mix(in oklch, oklch(0.65 0.2 25) 20%, transparent);
		box-shadow: 0 0 20px color-mix(in oklch, oklch(0.65 0.2 25) 15%, transparent);
	}

	.type-card-title {
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0;
	}

	.type-card-desc {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		margin: 0;
		line-height: 1.4;
	}

	/* ─── Responsive ─── */

	@media (max-width: 360px) {
		.selector-cards {
			grid-template-columns: 1fr;
		}

		.selector-modal {
			padding: 1.5rem;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.selector-backdrop,
		.selector-modal {
			animation: none;
		}
		.type-card {
			transition: none;
		}
	}
</style>
