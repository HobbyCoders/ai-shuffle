<script lang="ts">
	/**
	 * ConfirmDialog — a lightweight confirm/cancel dialog.
	 *
	 * Replaces ad-hoc inline confirmations and native `confirm()` (which blocks
	 * the page and is poor UX). Enter confirms, Escape / backdrop cancels.
	 *
	 * Usage:
	 *   <ConfirmDialog
	 *     open={!!pending}
	 *     title="Delete file"
	 *     message={`Delete "${pending?.name}"?`}
	 *     confirmLabel="Delete"
	 *     danger
	 *     onConfirm={doDelete}
	 *     onCancel={() => (pending = null)} />
	 */
	interface Props {
		open: boolean;
		title?: string;
		message: string;
		confirmLabel?: string;
		cancelLabel?: string;
		/** Style the confirm action as destructive. */
		danger?: boolean;
		/** Optional opt-in checkbox rendered between the message and the actions.
		 *  When set, a checkbox with this label appears; its state is exposed via
		 *  the bindable `checkboxChecked` prop so the caller can branch on confirm.
		 *  Omitted (undefined) → no checkbox, exactly as before. */
		checkboxLabel?: string;
		/** Two-way bound checked state for the optional opt-in checkbox. */
		checkboxChecked?: boolean;
		onConfirm: () => void;
		onCancel: () => void;
	}

	let {
		open,
		title = 'Confirm',
		message,
		confirmLabel = 'Confirm',
		cancelLabel = 'Cancel',
		danger = false,
		checkboxLabel = undefined,
		checkboxChecked = $bindable(false),
		onConfirm,
		onCancel
	}: Props = $props();

	/**
	 * Portal the overlay to document.body so its `position: fixed` box resolves
	 * against the viewport — NOT against an ancestor. A `backdrop-filter` (or
	 * `transform`/`filter`/`perspective`) on any ancestor makes that ancestor the
	 * containing block for fixed descendants, which would squeeze/clip this dialog
	 * inside that box (e.g. the chat input dock's `backdrop-filter: blur(20px)`).
	 * Reparenting to <body> escapes every such ancestor. Component-scoped style
	 * classes live on the elements themselves, so they travel intact.
	 * (Same idiom as MediaLightbox.)
	 */
	function portal(node: HTMLElement) {
		document.body.appendChild(node);
		return {
			destroy() {
				node.remove();
			}
		};
	}

	function onWindowKeydown(e: KeyboardEvent) {
		if (!open) return;
		if (e.key === 'Escape') {
			e.stopPropagation();
			onCancel();
		} else if (e.key === 'Enter') {
			e.stopPropagation();
			onConfirm();
		}
	}
</script>

<svelte:window onkeydown={onWindowKeydown} />

{#if open}
	<div
		use:portal
		class="fixed inset-0 z-200 flex items-center justify-center p-4"
		role="dialog"
		aria-modal="true"
		aria-labelledby="confirm-title"
		tabindex="-1"
	>
		<button
			class="absolute inset-0 bg-black/60 backdrop-blur-xs"
			onclick={onCancel}
			aria-label={cancelLabel}
		></button>

		<div
			class="relative w-full max-w-sm bg-card border border-border rounded-2xl shadow-2xl p-6 animate-confirm-in"
		>
			<h2 id="confirm-title" class="text-lg font-semibold text-foreground">{title}</h2>
			<p class="mt-2 text-sm text-muted-foreground whitespace-pre-line">{message}</p>

			{#if checkboxLabel}
				<label class="confirm-checkbox mt-4 flex items-center gap-2.5 text-sm text-foreground cursor-pointer select-none">
					<input type="checkbox" bind:checked={checkboxChecked} />
					<span>{checkboxLabel}</span>
				</label>
			{/if}

			<div class="mt-6 flex flex-col-reverse sm:flex-row items-stretch sm:items-center justify-end gap-3">
				<button
					onclick={onCancel}
					class="confirm-secondary-btn px-5 py-2.5 rounded-xl text-sm font-medium bg-muted text-foreground border border-border transition-colors"
				>
					{cancelLabel}
				</button>
				<button
					onclick={onConfirm}
					class="px-5 py-2.5 rounded-xl text-sm font-medium transition-all min-w-[96px]
						{danger
							? 'bg-destructive text-destructive-foreground hover:opacity-90'
							: 'bg-primary text-primary-foreground hover:opacity-90'}"
				>
					{confirmLabel}
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	@keyframes confirm-in {
		from {
			opacity: 0;
			transform: scale(0.96) translateY(8px);
		}
		to {
			opacity: 1;
			transform: scale(1) translateY(0);
		}
	}

	.animate-confirm-in {
		animation: confirm-in 160ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	@media (prefers-reduced-motion: reduce) {
		.animate-confirm-in {
			animation: none;
		}
	}

	.confirm-checkbox input[type='checkbox'] {
		width: 1rem;
		height: 1rem;
		flex-shrink: 0;
		accent-color: var(--destructive);
		cursor: pointer;
	}

	.confirm-checkbox input[type='checkbox']:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.confirm-secondary-btn:hover {
		background: var(--hover-overlay);
		border-color: color-mix(in oklch, var(--accent) 50%, var(--border));
	}

	.confirm-secondary-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}
</style>
