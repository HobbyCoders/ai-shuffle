<script lang="ts">
	/**
	 * SecurityInstructionsEditor: the chat instructions sent to every agent
	 * (`/settings/security-instructions`, the setting's former name), opened
	 * under Security > Chat instructions. Explicit Save; Revert to default goes
	 * through InlineConfirm and deletes the customization on the worker.
	 *
	 * A saved customization is never replaced by a newer built-in default, so
	 * the editor says whether the saved text still matches the default and can
	 * show the current default read-only for comparison.
	 */
	import InlineBanner from '../../rows/InlineBanner.svelte';
	import InlineConfirm from '../../rows/InlineConfirm.svelte';

	interface Props {
		/** The text being edited (bindable draft). */
		value: string;
		/** The saved text; `value !== saved` means unsaved edits. */
		saved: string;
		/** The built-in default of the running version. */
		defaultText: string;
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
		defaultText,
		customized,
		busy,
		changedElsewhere,
		error,
		onSave,
		onRevert,
		onLoadLatest
	}: Props = $props();

	const dirty = $derived(value !== saved);
	const differs = $derived(customized && saved !== defaultText);
	let showDefault = $state(false);
</script>

{#if changedElsewhere}
	<InlineBanner tone="warn">
		Changed elsewhere, your edits kept
		{#snippet actions()}
			<button type="button" class="st-btn sm" onclick={onLoadLatest}>Load latest</button>
		{/snippet}
	</InlineBanner>
{/if}
{#if customized}
	<div class="instr-note">
		<p class="st-note">
			{differs
				? 'Your saved text differs from the current default.'
				: 'Your saved text matches the current default.'}
		</p>
		{#if differs}
			<button
				type="button"
				class="st-btn ghost sm"
				aria-expanded={showDefault}
				onclick={() => (showDefault = !showDefault)}
			>
				{showDefault ? 'Hide default' : 'View default'}
			</button>
		{/if}
	</div>
{/if}
{#if differs && showDefault}
	<div class="st-xh">Current default</div>
	<textarea
		class="st-area tall"
		aria-label="Current default chat instructions"
		rows="12"
		readonly
		value={defaultText}
	></textarea>
	<div class="st-xh">Your text</div>
{/if}
<textarea
	class="st-area tall"
	aria-label="Chat instructions"
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
	.instr-note {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin-bottom: 8px;
	}
	.instr-note > .st-note {
		margin: 0 2px;
	}
</style>
