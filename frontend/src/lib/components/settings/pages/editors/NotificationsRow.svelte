<script lang="ts">
	import { onMount } from 'svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import ExpandToggle from '../../rows/ExpandToggle.svelte';
	import TextField from '../../rows/TextField.svelte';

	const access = getSettingsAccess();
	type PushStatus = { configured: boolean; devices_registered: number };
	let status = $state<PushStatus | null>(null);
	let open = $state(false);
	let key = $state('');
	let busy = $state(false);
	let error = $state('');
	const path = '/settings/notifications';
	const summary = $derived(status
		? `${status.configured ? 'Credentials set' : 'Push off'} · ${status.devices_registered} device${status.devices_registered === 1 ? '' : 's'}`
		: 'Checking push status…');
	onMount(() => {
		let disposed = false;
		api.get<PushStatus>(path).then((value) => { if (!disposed) status = value; })
			.catch((e) => { if (!disposed) error = getErrorMessage(e); });
		return () => { disposed = true; key = ''; };
	});
	async function save(remove = false) {
		if (busy || access.readOnly) return;
		busy = true; error = '';
		const value = key;
		key = ''; // Write-only: never retain the pasted secret after dispatch.
		try {
			status = remove ? await api.delete<PushStatus>(path)
				: await api.put<PushStatus>(path, { service_account: value });
			flashSaved();
		} catch (e) { error = getErrorMessage(e); }
		finally { busy = false; }
	}
</script>

<RowGroup title="Notifications">
	<SettingRow id="general.notifications" hint={error ? 'Status unavailable' : summary} expanded={open ? editor : undefined}>
		<ExpandToggle value={status?.configured ? 'Manage push' : 'Set up push'} bind:open />
	</SettingRow>
</RowGroup>
{#snippet editor()}
	<div class="push-editor">
		<p>Android alerts only when a chat needs you or an agent explicitly asks for you. No completion or parking alerts. The in-app bell is unchanged.</p>
		<p>Paste the Firebase sender service-account JSON. It is encrypted and synced across your workers, like provider billing keys. Leave it unset to keep push off.</p>
		<TextField type="password" label="Firebase sender service-account JSON" bind:value={key} maxlength={32768} placeholder="Paste service-account JSON" width={420} disabled={busy} />
		<div class="push-actions">
			<button class="st-btn" disabled={busy || access.readOnly || !key.trim()} onclick={() => save()}>Save credentials</button>
			{#if status?.configured}<button class="st-btn" disabled={busy || access.readOnly} onclick={() => save(true)}>Remove credentials</button>{/if}
		</div>
		{#if error}<p role="alert">{error}</p>{/if}
		<p>Requires a Firebase-configured Android build and phone notification permission. Registered devices are local to this worker. Sent does not mean seen.</p>
	</div>
{/snippet}

<style>
	.push-editor { display: grid; grid-template-columns: minmax(0, 1fr); min-width: 0; gap: 12px; padding: 8px 0 16px; color: var(--st-mfg); font-size: 13px; }
	.push-editor p { margin: 0; line-height: 1.5; }
	.push-actions { display: flex; flex-wrap: wrap; gap: 8px; }
</style>
