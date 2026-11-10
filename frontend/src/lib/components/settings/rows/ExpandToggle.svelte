<script lang="ts">
	/**
	 * ExpandToggle: the value button that opens an editor under its row
	 * ("Default ⌄"). Use inside a SettingRow and pass the editor as the row's
	 * `expanded` snippet while `open`:
	 *
	 *   <SettingRow id="general.themeStyle" expanded={open ? grid : undefined}>
	 *     <ExpandToggle value="Default" bind:open />
	 *   </SettingRow>
	 *
	 * A search target with `open: '<row id>'` opens it on arrival. Never
	 * disabled by read-only mode (the editor's fieldset is).
	 *
	 * `pending` draws the same button, disabled and busy, while the row's data
	 * loads, and `disabled` keeps it (not busy) when the value could not be
	 * read, so the row has one shape from loading to loaded or failed (a
	 * narrow page drops this button under its label; #1301).
	 */
	import { ChevronDown } from 'lucide-svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { getRowContext } from './row-context';

	interface Props {
		/** The current value shown on the button. */
		value: string;
		open: boolean;
		onToggle?: (open: boolean) => void;
		mono?: boolean;
		label?: string;
		/** The value is still loading: shown, but it cannot open yet. */
		pending?: boolean;
		/** The value could not be read: shown, but it cannot open. */
		disabled?: boolean;
	}

	let { value, open = $bindable(false), onToggle, mono = false, label, pending = false, disabled = false }: Props = $props();

	const row = getRowContext();
	const uid = $props.id();
	const valueId = `${uid}-value`;

	$effect(() => {
		const id = row?.id;
		if (id && !pending && !disabled && settingsNav.pendingOpen === id && settingsNav.consumeOpen(id) && !open) {
			open = true;
			onToggle?.(true);
		}
	});

	function toggle() {
		open = !open;
		onToggle?.(open);
	}
</script>

<button
	type="button"
	class="st-vbtn"
	aria-expanded={open}
	aria-controls={open ? row?.expansionId : undefined}
	aria-label={label ? `${label}: ${value}` : undefined}
	aria-labelledby={!label && row ? `${row.labelId} ${valueId}` : undefined}
	aria-describedby={row?.hintId}
	aria-busy={pending || undefined}
	disabled={pending || disabled}
	onclick={toggle}
>
	<span id={valueId} class:st-mono={mono}>{value}</span><ChevronDown />
</button>

<style>
	.st-vbtn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: 32px;
		padding: 0 9px 0 10px;
		border-radius: var(--st-radius-md);
		border: 1px solid var(--st-input);
		background: var(--st-field);
		font-size: 13px;
		white-space: nowrap;
		max-width: 260px;
	}
	.st-vbtn:disabled {
		color: var(--st-mfg);
		cursor: default;
	}
	.st-vbtn[aria-busy='true'] {
		cursor: progress;
	}
	/* It cannot open yet (or at all): the chevron fades, as a disabled control's does. */
	.st-vbtn:disabled > :global(svg) {
		opacity: 0.4;
	}
	.st-vbtn > span {
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.st-vbtn :global(svg.lucide) {
		width: 14px;
		height: 14px;
		color: var(--st-mfg);
		transition: transform 0.15s;
	}
	.st-vbtn[aria-expanded='true'] :global(svg.lucide) {
		transform: rotate(180deg);
	}
</style>
