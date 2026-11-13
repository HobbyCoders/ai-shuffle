<script lang="ts">
	/**
	 * A write-only credential field. A stored secret reads "Saved" with Replace,
	 * and nothing about the stored value is ever rendered, placeholders included.
	 * Replace opens a blank field; Cancel leaves the stored secret as it is.
	 * `actions` adds buttons beside Replace (Check key), so one stored key is
	 * one row.
	 */
	import { tick, type Snippet } from 'svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import TextField from '../rows/TextField.svelte';
	import ValueText from '../rows/ValueText.svelte';

	interface Props {
		label: string;
		saved: boolean;
		/** Placeholder when nothing is stored yet. */
		placeholder?: string;
		busy?: boolean;
		/** Resolves truthy once the new secret is stored. */
		onsave: (value: string) => Promise<unknown>;
		/** Hint while not replacing. */
		hint?: string;
		/** More buttons beside Replace while a secret is stored. */
		actions?: Snippet;
	}

	let { label, saved, placeholder = '', busy = false, onsave, hint, actions }: Props = $props();

	let replacing = $state(false);
	let value = $state('');
	let wrap: HTMLElement | undefined = $state();
	const editing = $derived(!saved || replacing);

	async function startReplace() {
		replacing = true;
		await tick();
		wrap?.querySelector('input')?.focus();
	}

	function cancel() {
		replacing = false;
		value = '';
	}

	async function submit() {
		const next = value.trim();
		if (!next) return;
		if (await onsave(next)) cancel();
	}
</script>

<SettingRow {label} hint={replacing ? 'The saved one stays until you save a new one' : hint}>
	{#if editing}
		<span class="edit" bind:this={wrap}>
			<TextField
				bind:value
				type="password"
				mono
				width={260}
				label={saved ? `New ${label.toLowerCase()}` : label}
				placeholder={saved ? 'New value' : placeholder}
				autocomplete="new-password"
				disabled={busy}
			/>
			<button type="button" class="st-btn" disabled={busy || !value.trim()} onclick={submit}>Save</button>
			{#if replacing}<button type="button" class="st-btn ghost" disabled={busy} onclick={cancel}>Cancel</button>{/if}
		</span>
	{:else}
		<ValueText value="Saved" />
		<button type="button" class="st-btn" disabled={busy} onclick={startReplace}>Replace</button>
		{@render actions?.()}
	{/if}
</SettingRow>

<style>
	.edit {
		display: inline-flex;
		flex-wrap: wrap;
		justify-content: flex-end;
		align-items: center;
		gap: 8px;
		min-width: 0;
	}
</style>
