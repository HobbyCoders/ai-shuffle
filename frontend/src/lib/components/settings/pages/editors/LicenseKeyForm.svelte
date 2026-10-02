<script lang="ts">
	/**
	 * LicenseKeyForm: plan, last check with Check now, and the key entry,
	 * opened under System > License > License key. A secret, so Activate is
	 * explicit (Enter submits).
	 */
	import { RefreshCw } from 'lucide-svelte';
	import { planName, type LicenseStatus } from '$lib/api/license';
	import SettingRow from '../../rows/SettingRow.svelte';
	import ValueText from '../../rows/ValueText.svelte';

	interface Props {
		status: LicenseStatus;
		/** A lease is held: show plan, last check and Check now. */
		hasLease: boolean;
		busy: boolean;
		key: string;
		onActivate: () => void;
		onCheck: () => void;
	}

	let { status, hasLease, busy, key = $bindable(''), onActivate, onCheck }: Props = $props();

	function when(iso: string | null): string {
		if (!iso) return 'Never';
		const date = new Date(iso);
		return Number.isNaN(date.getTime()) ? iso : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
	}
</script>

<form
	class="st-xlist"
	aria-label="License key"
	onsubmit={(e) => {
		e.preventDefault();
		if (key.trim()) onActivate();
	}}
>
	{#if hasLease}
		<SettingRow label="Plan">
			<ValueText value={planName(status.plan)} tone="fg" />
		</SettingRow>
		<SettingRow label="Last checked" hint={status.last_error ? 'Last check failed' : undefined}>
			<ValueText value={when(status.last_refresh_at)} />
			<button type="button" class="st-btn" aria-label="Check the license now" disabled={busy} onclick={onCheck}><RefreshCw />Check now</button>
		</SettingRow>
	{/if}
	<SettingRow label={hasLease ? 'Use a different key' : 'Key'}>
		<input
			class="st-txt key st-mono"
			aria-label="License key"
			placeholder="ASH-XXXXX-XXXXX-XXXXX-XXXXX"
			autocomplete="off"
			spellcheck="false"
			bind:value={key}
			disabled={busy}
		/>
		<button type="submit" class="st-btn primary" disabled={busy || !key.trim()}>{busy ? 'Working…' : 'Activate'}</button>
	</SettingRow>
</form>
{#if status.last_error}<p class="st-note">Last check failed: {status.last_error}</p>{/if}

<style>
	.key {
		height: 32px;
		width: 250px;
		max-width: 100%;
		min-width: 0;
		border-radius: var(--st-radius-md);
		border: 1px solid var(--st-input);
		background: var(--st-muted);
		color: var(--st-fg);
		padding: 0 10px;
	}
	.key::placeholder {
		color: var(--st-subtle);
	}
	.key:focus {
		outline: none;
		border-color: var(--st-primary);
		box-shadow: 0 0 0 3px var(--st-primary-soft);
	}
</style>
