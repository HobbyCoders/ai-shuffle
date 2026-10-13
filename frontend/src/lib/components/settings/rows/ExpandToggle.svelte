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
	}

	let { value, open = $bindable(false), onToggle, mono = false, label }: Props = $props();

	const row = getRowContext();
	const uid = $props.id();
	const valueId = `${uid}-value`;

	$effect(() => {
		const id = row?.id;
		if (id && settingsNav.pendingOpen === id && settingsNav.consumeOpen(id) && !open) {
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
	onclick={toggle}
>
	<span id={valueId} class:st-mono={mono}>{value}</span><ChevronDown />
</button>

<style>
	.st-vbtn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: 30px;
		padding: 0 8px 0 10px;
		border-radius: var(--st-radius-md);
		border: 1px solid var(--st-input);
		background: var(--st-muted);
		font-size: 13px;
		white-space: nowrap;
		max-width: 260px;
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
	.st-vbtn:hover {
		border-color: var(--st-border-strong);
	}
	.st-vbtn[aria-expanded='true'] :global(svg.lucide) {
		transform: rotate(180deg);
	}
</style>
