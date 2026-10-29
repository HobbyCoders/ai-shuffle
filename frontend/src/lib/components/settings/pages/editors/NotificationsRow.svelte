<script lang="ts">
	import { onMount } from 'svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { ACCOUNT_CHANGED_EVENT } from '$lib/api/account';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';

	type PushStatus = { linked: boolean; devices: number; enabled: boolean };
	let status = $state<PushStatus | null>(null);
	let error = $state('');
	const summary = $derived(error ? 'Phone notification status unavailable' : !status
		? 'Checking phone notifications…'
		: status.linked
			? `Phone notifications: ${status.enabled ? 'on' : 'off'} · ${status.devices} device${status.devices === 1 ? '' : 's'}`
			: 'Sign in to your HobbyCoders account to get phone notifications');

	onMount(() => {
		let disposed = false;
		let revision = 0;
		async function refresh() {
			const request = ++revision;
			try {
				const value = await api.get<PushStatus>('/settings/notifications');
				if (!disposed && request === revision) { status = value; error = ''; }
			} catch (e) {
				if (!disposed && request === revision) error = getErrorMessage(e);
			}
		}
		void refresh();
		window.addEventListener(ACCOUNT_CHANGED_EVENT, refresh);
		return () => { disposed = true; window.removeEventListener(ACCOUNT_CHANGED_EVENT, refresh); };
	});
</script>

<RowGroup title="Notifications">
	<SettingRow id="general.notifications" label={summary}
		help="Android alerts only when a chat needs you. Requires phone notification permission. The in-app bell and local use are unchanged. Sent does not mean seen.">
		{#if status && !status.linked}
			<button class="st-btn" onclick={() => settingsNav.go({ category: 'devices', item: 'account' })}>Sign in</button>
		{/if}
	</SettingRow>
	{#if error}<p class="status-error" role="alert">{error}</p>{/if}
</RowGroup>

<style>
	.status-error { color: var(--st-mfg); font-size: 13px; }
</style>
