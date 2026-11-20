<script lang="ts">
	import { onMount } from 'svelte';
	import type { DesktopLoginItem, DesktopLoginItemState, DesktopPlatform } from '$lib/services/desktop';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import ActionButton from '../rows/ActionButton.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';

	let { loginItem, platform }: { loginItem: DesktopLoginItem; platform: DesktopPlatform } = $props();
	const access = getSettingsAccess();
	type ConfirmedStatus = Exclude<DesktopLoginItemState, 'unavailable'>;
	let status = $state<ConfirmedStatus | null>(null);
	let pending = $state<'read' | 'write' | null>(null);
	const busy = $derived(pending !== null);
	let refreshQueued = false;
	let mounted = false;
	let error = $state<string>();
	const osSettings = $derived(platform === 'darwin'
		? 'System Settings → General → Login Items & Extensions'
		: 'Windows Settings → Apps → Startup');
	const hint = $derived(pending === 'write' ? 'Saving startup setting…'
		: pending === 'read' ? 'Checking startup settings…'
		: error ?? (status === 'blocked' ? `Startup is off. To allow it, check ${osSettings}.`
			: status === null ? 'Checking startup settings…' : undefined));

	function isConfirmed(value: unknown): value is ConfirmedStatus {
		return value === 'enabled' || value === 'disabled' || value === 'blocked';
	}

	function finish() {
		pending = null;
		if (!mounted || !refreshQueued) return;
		refreshQueued = false;
		void refresh(true);
	}

	async function refresh(keepError = false) {
		if (!mounted) return;
		if (busy) {
			refreshQueued = true;
			return;
		}
		pending = 'read';
		try {
			const current = await loginItem.get();
			if (!isConfirmed(current)) {
				status = null;
				error = current === 'unavailable' ? 'Startup is unavailable for this installation.' : 'Could not read the startup setting.';
			} else {
				if (!keepError || current !== status) error = undefined;
				status = current;
			}
		} catch {
			status = null;
			error = 'Could not read the startup setting.';
		} finally {
			finish();
		}
	}

	async function change(enabled: boolean) {
		if (busy || access.readOnly || status === null) return;
		pending = 'write';
		error = undefined;
		try {
			const result = await loginItem.set(enabled);
			if (isConfirmed(result?.state)) {
				status = result.state;
				if ((result.failed || status !== (enabled ? 'enabled' : 'disabled')) && (!enabled || status !== 'blocked')) {
					error = `Could not ${enabled ? 'turn on' : 'turn off'} startup. Check ${osSettings}.`;
				}
			} else {
				unconfirmedWrite();
			}
		} catch {
			unconfirmedWrite();
		} finally {
			finish();
		}
	}

	function unconfirmedWrite() {
		status = null;
		error = 'Could not confirm the startup setting. Try reading it again.';
	}

	onMount(() => {
		mounted = true;
		void refresh();
		return () => { mounted = false; };
	});
</script>

<svelte:window onfocus={() => refresh()} />

<RowGroup title="Desktop app">
	<SettingRow id="general.startAtSignIn" {hint} wrapHint>
		<span class="sr-only" role="status">{hint ?? ''}</span>
		{#if status === 'blocked'}
			<ActionButton disabled={busy || access.readOnly} onclick={() => change(false)}>Remove startup item</ActionButton>
		{:else if status === null && error}
			<ActionButton disabled={busy} onclick={() => refresh()}>Try again</ActionButton>
		{/if}
		{#if status !== null}
			<Switch controlled checked={status === 'enabled'} disabled={busy} onchange={change} />
		{/if}
	</SettingRow>
</RowGroup>
