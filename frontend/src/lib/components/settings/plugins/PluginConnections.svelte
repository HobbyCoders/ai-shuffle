<script lang="ts">
	/**
	 * PluginConnections: the outside services an installed plugin declares.
	 * A service with an OAuth sign-in gets its state, Sign in / Sign out and,
	 * only when no client is available, the manual setup. Services without a
	 * sign-in are listed as before ("Asks you for access").
	 */
	import { untrack } from 'svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { getSignInStatus, type PluginSignInStatus } from '$lib/api/plugin-oauth';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import PluginServiceRow from './PluginServiceRow.svelte';

	interface Props {
		pluginId: string;
		/** Service names the plugin declares (from its components). */
		services: string[];
		/** Switched on; a change re-reads the sign-in state. */
		enabled: boolean;
	}

	let { pluginId, services, enabled }: Props = $props();

	let status = $state<PluginSignInStatus | null>(null);
	let loading = $state(true);
	let error = $state('');

	async function load(): Promise<PluginSignInStatus | null> {
		try {
			status = await getSignInStatus(pluginId);
			error = '';
			return status;
		} catch (e) {
			// 404: nothing here declares a sign-in; list the names only.
			status = null;
			error = (e as { status?: number })?.status === 404 ? '' : getErrorMessage(e, 'unavailable');
			return null;
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		void pluginId;
		void enabled;
		untrack(() => void load());
	});

	const signIns = $derived(status?.services ?? []);
	const plain = $derived(services.filter((name) => !signIns.some((s) => s.service === name)));
	const count = $derived(signIns.length + plain.length);
</script>

<div data-testid="plugin-connections">
	<RowGroup title="Connections" count={count}>
		{#if loading && !status}
			<SettingRow label="Checking sign-ins…" />
		{:else if error}
			<SettingRow label="Sign-in status unavailable" hint="Try again in a moment">
				<button type="button" class="st-btn sm" onclick={() => load()}>Retry</button>
			</SettingRow>
		{/if}
		{#each signIns as entry (entry.service)}
			<PluginServiceRow {pluginId} {entry} enabled={status?.enabled ?? false} redirectUri={status?.redirect_uri ?? ''}
				relayUri={status?.relay_uri ?? ''} onChanged={load} />
		{/each}
		{#each plain as name (name)}
			<SettingRow label={name} hint="Asks you for access" />
		{/each}
	</RowGroup>
</div>
