<script lang="ts">
	/**
	 * Test harness: a SettingRow with a Switch (or an InlineConfirm) inside,
	 * optionally under read-only Settings access.
	 */
	import { setContext } from 'svelte';
	import { SETTINGS_ACCESS_CONTEXT } from '$lib/settings/access.svelte';
	import SettingRow from '$lib/components/settings/rows/SettingRow.svelte';
	import Switch from '$lib/components/settings/rows/Switch.svelte';
	import InlineConfirm from '$lib/components/settings/rows/InlineConfirm.svelte';

	interface Props {
		id?: string;
		label?: string;
		hint?: string;
		help?: string;
		value?: unknown;
		defaultValue?: unknown;
		onReset?: () => void;
		checked?: boolean;
		onchange?: (v: boolean) => void;
		readOnly?: boolean;
		confirm?: { label: string; question: string; onConfirm: () => void | Promise<void> };
		expandedText?: string;
	}

	let {
		id,
		label,
		hint,
		help,
		value,
		defaultValue,
		onReset,
		checked = false,
		onchange,
		readOnly = false,
		confirm,
		expandedText
	}: Props = $props();

	setContext(SETTINGS_ACCESS_CONTEXT, {
		get readOnly() {
			return readOnly;
		}
	});
</script>

<SettingRow {id} {label} {hint} {help} {value} {defaultValue} {onReset} expanded={expandedText ? extra : undefined}>
	{#if confirm}
		<InlineConfirm label={confirm.label} question={confirm.question} onConfirm={confirm.onConfirm} />
	{:else}
		<Switch {checked} {onchange} />
	{/if}
</SettingRow>

{#snippet extra()}
	<p>{expandedText}</p>
	<button type="button">Inside</button>
{/snippet}
