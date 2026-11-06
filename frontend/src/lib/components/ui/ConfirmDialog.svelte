<script lang="ts">
	import '$lib/components/ui/surfaces.css';
	import Checkbox from './Checkbox.svelte';
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
	import { focusTrap } from './focus-trap';

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
			e.preventDefault();
			e.stopPropagation();
			onCancel();
		} else if (e.key === 'Enter') {
			// Enter on a focused button or link is that control's own activation
			// (e.g. Cancel), not a confirm.
			const target = e.target instanceof Element ? e.target : null;
			if (target?.closest('button, a[href]')) return;
			e.stopPropagation();
			onConfirm();
		}
	}
</script>

<svelte:window onkeydown={onWindowKeydown} />

{#if open}
	<div
		use:portal
		use:focusTrap={{ initial: '[data-confirm-action]' }}
		class="ui-overlay ui-surface"
		role="dialog"
		aria-modal="true"
		aria-labelledby="confirm-title"
		tabindex="-1"
	>
		<button
			class="ui-scrim"
			onclick={onCancel}
			tabindex="-1"
			aria-label={cancelLabel}
		></button>

		<div
			class="ui-dialog"
		>
			<header class="ui-dialog-header"><h2 id="confirm-title" class="ui-dialog-title">{title}</h2></header>
			<div class="ui-dialog-body"><p class="mt-2 text-sm text-muted-foreground whitespace-pre-line">{message}</p>

			{#if checkboxLabel}
<Checkbox bind:checked={checkboxChecked}>{checkboxLabel}</Checkbox>
			{/if}

			</div>
			<div class="ui-dialog-footer">
				<button
					onclick={onCancel}
					class="ui-button"
				>
					{cancelLabel}
				</button>
				<button
					onclick={onConfirm}
					data-confirm-action
					class="ui-button {danger ? 'danger' : 'primary'}"
				>
					{confirmLabel}
				</button>
			</div>
		</div>
	</div>
{/if}
