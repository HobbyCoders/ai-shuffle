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
			<div class="selector-header">
				<span class="selector-eyebrow">Start</span>
				<h2 class="selector-heading">New session</h2>
				<p class="selector-subtext">Choose how you want to interact with Claude.</p>
			</div>

			<div class="selector-cards">
				<!-- Chat card -->
				<button
					class="type-card"
					onclick={() => handleSelect('chat')}
				>
					<div class="type-card-icon">
						<MessageSquare size={26} strokeWidth={1.75} />
					</div>
					<div class="type-card-body">
						<h3 class="type-card-title">Chat</h3>
						<p class="type-card-desc">Text conversation</p>
					</div>
					<span class="type-card-arrow" aria-hidden="true">→</span>
				</button>

				<!-- Voice card -->
				<button
					class="type-card"
					onclick={() => handleSelect('voice')}
				>
					<div class="type-card-icon">
						<Mic size={26} strokeWidth={1.75} />
					</div>
					<div class="type-card-body">
						<h3 class="type-card-title">Voice</h3>
						<p class="type-card-desc">Real-time audio</p>
					</div>
					<span class="type-card-arrow" aria-hidden="true">→</span>
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
		background: var(--modal-overlay, color-mix(in oklch, var(--background) 70%, transparent));
		backdrop-filter: blur(10px);
		-webkit-backdrop-filter: blur(10px);
		animation: selector-fade-in 0.2s ease-out;
	}

	@keyframes selector-fade-in {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	.selector-modal {
		position: relative;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 1rem;
		padding: 1.75rem;
		max-width: 460px;
		width: 100%;
		box-shadow: var(--shadow-l, 0 10px 40px rgba(0, 0, 0, 0.35));
		animation: selector-slide-up 0.25s cubic-bezier(0.16, 1, 0.3, 1);
		overflow: hidden;
	}

	/* Subtle top accent line */
	.selector-modal::before {
		content: '';
		position: absolute;
		top: 0;
		left: 1.75rem;
		right: 1.75rem;
		height: 1px;
		background: linear-gradient(
			90deg,
			transparent,
			color-mix(in oklch, var(--primary) 40%, transparent),
			transparent
		);
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

	.selector-header {
		margin-bottom: 1.25rem;
	}

	.selector-eyebrow {
		display: inline-block;
		font-size: 0.6875rem;
		font-weight: 600;
		letter-spacing: 0.12em;
		text-transform: uppercase;
		color: var(--muted-foreground);
		margin-bottom: 0.375rem;
	}

	.selector-heading {
		font-size: 1.375rem;
		font-weight: 700;
		color: var(--foreground);
		margin: 0 0 0.25rem;
		letter-spacing: -0.02em;
		line-height: 1.15;
	}

	.selector-subtext {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		margin: 0;
		line-height: 1.5;
	}

	.selector-cards {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	/* ─── Type Card ─── */

	.type-card {
		position: relative;
		display: flex;
		align-items: center;
		gap: 0.875rem;
		padding: 0.875rem 1rem;
		background: color-mix(in oklch, var(--muted) 40%, transparent);
		border: 1px solid var(--border);
		border-radius: 0.75rem;
		cursor: pointer;
		transition:
			background-color 0.2s cubic-bezier(0.4, 0, 0.2, 1),
			border-color 0.2s cubic-bezier(0.4, 0, 0.2, 1),
			transform 0.2s cubic-bezier(0.4, 0, 0.2, 1),
			box-shadow 0.2s cubic-bezier(0.4, 0, 0.2, 1);
		text-align: left;
		color: var(--foreground);
		width: 100%;
	}

	.type-card:hover {
		border-color: color-mix(in oklch, var(--primary) 50%, var(--border));
		background: color-mix(in oklch, var(--primary) 6%, var(--card));
		transform: translateY(-1px);
		box-shadow:
			0 1px 0 color-mix(in oklch, var(--primary) 15%, transparent) inset,
			0 6px 18px color-mix(in oklch, var(--primary) 12%, transparent);
	}

	.type-card:focus-visible {
		outline: none;
		border-color: var(--primary);
		box-shadow: 0 0 0 2px var(--ring);
	}

	.type-card:active {
		transform: translateY(0);
	}

	.type-card-icon {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		border-radius: 0.625rem;
		background: color-mix(in oklch, var(--muted) 70%, transparent);
		border: 1px solid color-mix(in oklch, var(--border) 80%, transparent);
		color: var(--muted-foreground);
		transition:
			background-color 0.2s ease,
			color 0.2s ease,
			border-color 0.2s ease;
	}

	.type-card:hover .type-card-icon {
		background: color-mix(in oklch, var(--primary) 14%, var(--card));
		border-color: color-mix(in oklch, var(--primary) 35%, var(--border));
		color: var(--primary);
	}

	.type-card-body {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 0.125rem;
	}

	.type-card-title {
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0;
		letter-spacing: -0.01em;
	}

	.type-card-desc {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		margin: 0;
		line-height: 1.3;
	}

	.type-card-arrow {
		flex-shrink: 0;
		font-size: 1rem;
		color: var(--muted-foreground);
		opacity: 0.5;
		transform: translateX(-4px);
		transition:
			transform 0.2s cubic-bezier(0.4, 0, 0.2, 1),
			opacity 0.2s ease,
			color 0.2s ease;
	}

	.type-card:hover .type-card-arrow {
		color: var(--primary);
		opacity: 1;
		transform: translateX(0);
	}

	/* ─── Responsive ─── */

	@media (max-width: 360px) {
		.selector-modal {
			padding: 1.25rem;
		}

		.type-card {
			padding: 0.75rem 0.875rem;
			gap: 0.75rem;
		}

		.type-card-icon {
			width: 40px;
			height: 40px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.selector-backdrop,
		.selector-modal {
			animation: none;
		}
		.type-card,
		.type-card-icon,
		.type-card-arrow {
			transition: none;
		}
		.type-card:hover {
			transform: none;
		}
	}
</style>
