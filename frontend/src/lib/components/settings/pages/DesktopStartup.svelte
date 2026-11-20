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
	let status = $state<DesktopLoginItemState | null>(null);
	let busy = $state(false);
	let refreshQueued = false;
	let mounted = false;
	let error = $state<string>();
	const osSettings = $derived(platform === 'darwin'
		? 'System Settings → General → Login Items & Extensions'
		: 'Windows Settings → Apps → Startup');
	const hint = $derived(error ?? (status === 'blocked'
		? `Startup is off. To allow it, check ${osSettings}.`
		: status === 'unavailable' ? 'Startup is unavailable for this installation.'
		: status === null ? 'Checking startup settings…' : undefined));

	function finish() {
		busy = false;
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
		busy = true;
		try {
			const current = await loginItem.get();
			if (!keepError || current !== status) error = undefined;
			status = current;
		} catch {
			status = null;
			error = 'Could not read the startup setting.';
		} finally {
			finish();
		}
	}

	async function change(enabled: boolean) {
		if (busy || access.readOnly || status === null || status === 'unavailable') return;
		busy = true;
		error = undefined;
		try {
			const result = await loginItem.set(enabled);
			status = result.state;
			if (result.failed && (!enabled || status !== 'blocked')) {
				error = `Could not ${enabled ? 'turn on' : 'turn off'} startup. Check ${osSettings}.`;
			}
		} catch {
			status = null;
			error = 'Could not confirm the startup setting. Try reading it again.';
		} finally {
			finish();
		}
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
		<Switch controlled checked={status === 'enabled'} disabled={busy || status === null || status === 'unavailable'} onchange={change} />
	</SettingRow>
</RowGroup>
