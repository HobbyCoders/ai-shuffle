<script lang="ts">
	/**
	 * SecurityInstructionsEditor: the security instructions sent to every
	 * agent (`/settings/security-instructions`), opened under Security >
	 * Security instructions. Explicit Save; Revert to default goes through
	 * InlineConfirm and deletes the customization on the worker.
	 */
	import InlineBanner from '../../rows/InlineBanner.svelte';
	import InlineConfirm from '../../rows/InlineConfirm.svelte';

	interface Props {
		/** The text being edited (bindable draft). */
		value: string;
		/** The saved text; `value !== saved` means unsaved edits. */
		saved: string;
		customized: boolean;
		busy: boolean;
		/** A newer saved text arrived while there were unsaved edits. */
		changedElsewhere: boolean;
		error: string;
		onSave: () => void;
		onRevert: () => Promise<void> | void;
		onLoadLatest: () => void;
	}

	let {
		value = $bindable(),
		saved,
		customized,
		busy,
		changedElsewhere,
		error,
		onSave,
		onRevert,
		onLoadLatest
	}: Props = $props();

	const dirty = $derived(value !== saved);
</script>

{#if changedElsewhere}
	<InlineBanner tone="warn">
		Changed elsewhere, your edits kept
		{#snippet actions()}
			<button type="button" class="st-btn sm" onclick={onLoadLatest}>Load latest</button>
		{/snippet}
	</InlineBanner>
{/if}
<textarea
	class="st-area tall"
	aria-label="Security instructions"
	placeholder="Instructions for every agent"
	rows="12"
	bind:value
	disabled={busy}
></textarea>
{#if error}<p class="st-msg err" role="alert">{error}</p>{/if}
<div class="st-acts">
	{#if dirty}<span class="st-badge warn st-left">Unsaved changes</span>{/if}
	<InlineConfirm
		label="Revert to default"
		question="Discard customizations?"
		confirmLabel="Revert"
		variant="ghost"
		disabled={busy || !customized}
		onConfirm={onRevert}
	/>
	<button type="button" class="st-btn primary" disabled={busy || !dirty} onclick={onSave}>
		{busy ? 'Saving…' : 'Save changes'}
	</button>
</div>

<style>
	.tall {
		min-height: 220px;
	}
</style>
