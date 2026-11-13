<script lang="ts">
	/**
	 * InlineConfirm: the one confirm for destructive or risky actions. The
	 * trigger swaps in place for "question [Confirm] [Cancel]"; focus moves to
	 * Cancel, Esc cancels and returns focus to the trigger. After Confirm, focus
	 * returns to the trigger while it is still on the page (a failed action).
	 * `onArm` runs as the question opens.
	 *
	 * Default trigger is a button labelled `label`. For a guarded switch, pass
	 * `trigger` and call the `arm` it receives:
	 *
	 *   <InlineConfirm question="Any device becomes owner?" confirmLabel="Trust network" onConfirm={enable}>
	 *     {#snippet trigger(arm)}<Switch checked={on} onchange={(v) => (v ? arm() : disable())} />{/snippet}
	 *   </InlineConfirm>
	 */
	import { tick, type Snippet } from 'svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';

	interface Props {
		/** Trigger text (default trigger only). */
		label?: string;
		question: string;
		/** Confirm button text; defaults to `label`. */
		confirmLabel?: string;
		/** danger: red question and solid red confirm. primary: plain question, teal confirm. */
		tone?: 'danger' | 'primary';
		/** Look of the default trigger. */
		variant?: 'danger' | 'ghost' | 'default' | 'warn' | 'primary';
		size?: 'md' | 'sm';
		onConfirm: () => void | Promise<void>;
		onCancel?: () => void;
		onArm?: () => void;
		disabled?: boolean;
		armed?: boolean;
		trigger?: Snippet<[arm: () => void]>;
	}

	let {
		label = '',
		question,
		confirmLabel,
		tone = 'danger',
		variant = 'danger',
		size = 'md',
		onConfirm,
		onCancel,
		onArm,
		disabled = false,
		armed = $bindable(false),
		trigger,
		...rest
	}: Props & { [key: `data-${string}`]: string } = $props();

	const access = getSettingsAccess();
	const off = $derived(disabled || access.readOnly);
	let busy = $state(false);
	let wrap: HTMLSpanElement | undefined = $state();
	let cancelButton: HTMLButtonElement | undefined = $state();

	async function arm() {
		if (off) return;
		armed = true;
		onArm?.();
		await tick();
		cancelButton?.focus();
	}

	function focusTrigger() {
		wrap?.querySelector<HTMLElement>('button:not([disabled]), [role="switch"]')?.focus();
	}

	async function disarm(refocus: boolean) {
		armed = false;
		onCancel?.();
		if (!refocus) return;
		await tick();
		focusTrigger();
	}

	async function confirm() {
		if (busy) return;
		busy = true;
		const hadFocus = !!wrap && wrap.contains(document.activeElement);
		try {
			await onConfirm();
		} finally {
			busy = false;
			armed = false;
			if (hadFocus) void refocusAfterConfirm();
		}
	}

	/** The confirm buttons are gone; without this focus drops to the page. */
	async function refocusAfterConfirm() {
		await tick();
		const lost = !document.activeElement || document.activeElement === document.body;
		if (lost && wrap?.isConnected) focusTrigger();
	}

	function onkeydown(event: KeyboardEvent) {
		if (event.key === 'Escape' && armed) {
			event.preventDefault();
			event.stopPropagation();
			void disarm(true);
		}
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<span class="st-confirm" bind:this={wrap} {onkeydown} {...rest}>
	{#if armed}
		<span class="st-ask" class:plain={tone === 'primary'} role="status">{question}</span>
		<button
			type="button"
			class="st-btn {tone === 'danger' ? 'danger solid' : 'primary'}"
			class:sm={size === 'sm'}
			disabled={busy}
			onclick={confirm}>{confirmLabel ?? label}</button
		>
		<button
			bind:this={cancelButton}
			type="button"
			class="st-btn ghost"
			class:sm={size === 'sm'}
			disabled={busy}
			onclick={() => disarm(true)}>Cancel</button
		>
	{:else if trigger}
		{@render trigger(arm)}
	{:else}
		<button
			type="button"
			class="st-btn"
			class:danger={variant === 'danger'}
			class:ghost={variant === 'ghost'}
			class:warn={variant === 'warn'}
			class:primary={variant === 'primary'}
			class:sm={size === 'sm'}
			disabled={off}
			onclick={arm}>{label}</button
		>
	{/if}
</span>

<style>
	.st-confirm {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
		justify-content: flex-end;
	}
	.st-ask {
		font-size: 13px;
		color: var(--st-destructive);
		font-weight: 500;
		white-space: nowrap;
	}
	.st-ask.plain {
		color: var(--st-fg);
	}
</style>
