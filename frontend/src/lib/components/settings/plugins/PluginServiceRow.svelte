<script lang="ts">
	/**
	 * PluginServiceRow: one plugin service with an OAuth sign-in. The state
	 * in a few words, Sign in (one button, at the provider) or Sign out
	 * (confirmed in place), and the manual client setup tucked behind
	 * "Set up manually" only when signInView says a client is needed or the
	 * owner entered the current one. A plugin's own client that returns
	 * through the account hub says so under the row; one this worker can't
	 * use falls back to the manual setup with the reason.
	 */
	import { onDestroy, onMount, untrack } from 'svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { isBundledMobile } from '$lib/api/runtime';
	import { deleteClient, signOut, type PluginSignInStatus, type ServiceSignIn } from '$lib/api/plugin-oauth';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import ClientSetupForm from '$lib/components/plugins/ClientSetupForm.svelte';
	import {
		INSECURE_ADDRESS, SignInFlow, accessText, ownerClientFallback, relayBlocked, relayHost, relayLine, relayStartFailed, relayText, relayWaitText,
		returnAddressUsable, signInView, stateHint
	} from '$lib/components/plugins/plugin-sign-in.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';
	import StatusDot from '../rows/StatusDot.svelte';

	interface Props {
		pluginId: string;
		entry: ServiceSignIn;
		/** The plugin is switched on and approved. */
		enabled: boolean;
		redirectUri: string;
		/** The account hub's fixed return address (`''` when absent). */
		relayUri?: string;
		/** Re-read the plugin's sign-in status. */
		onChanged: () => Promise<PluginSignInStatus | null>;
	}

	let { pluginId, entry, enabled, redirectUri, relayUri = '', onChanged }: Props = $props();
	const access = getSettingsAccess();
	const view = $derived(signInView(entry, enabled));
	const host = $derived(entry.authorize_host ?? '');
	let manualOpen = $state(false);
	let error = $state('');
	let mobileApp = $state(false);
	const flow = new SignInFlow(untrack(() => entry.plugin_id || pluginId), untrack(() => entry.service), async () => {
		const next = await onChanged();
		return next?.services.find((s) => s.service === entry.service)?.state ?? null;
	});

	onMount(() => { mobileApp = isBundledMobile(); });
	onDestroy(() => flow.dispose());

	const returnOk = $derived(!redirectUri || returnAddressUsable(redirectUri));
	/** The plugin's own client can't be used from this worker or address. */
	const blocked = $derived(relayBlocked(entry));
	/** The host a relayed sign-in returns through, when it does. */
	const via = $derived(relayHost({ relay_uri: relayUri }, entry));
	const waitVia = $derived(flow.relayed === false ? '' : flow.relayHost || via);
	/** The account service refused or couldn't take a relayed sign-in: offer the manual setup. */
	const relayFallback = $derived(relayStartFailed(entry, flow));
	const plugin = $derived(entry.plugin_id || pluginId);
	const tone = $derived(
		view.step === 'signed_in' ? 'ok'
			: flow.phase === 'waiting' ? 'busy'
				: view.step === 'invalid' ? 'err'
					: view.again || view.step === 'setup' ? 'warn' : 'off'
	);
	const hint = $derived(
		flow.phase === 'waiting' ? 'Waiting for sign-in'
			: (view.step === 'signed_in' || view.step === 'sign_in') && host && !view.again
				? `${stateHint(view, entry.reason)} · ${host}`
				: blocked && view.step === 'setup' && !view.again
					? relayText(entry.relay_reason ?? '', plugin, true)
					: stateHint(view, entry.reason)
	);
	/** A line under the row: why sign-in is blocked, or how it ended. */
	const note = $derived(
		flow.phase === 'error' ? flow.error
			: error ? error
				: flow.phase === 'waiting' && waitVia ? relayWaitText(waitVia)
					: blocked && view.step === 'setup' ? relayText(entry.relay_reason ?? '', plugin, false, 'row')
						: view.step !== 'sign_in' ? ''
							: mobileApp ? 'Sign in from a browser or the desktop app.'
								: !returnOk ? INSECURE_ADDRESS.row
									: flow.unfinished ? 'Not finished yet. If you closed the window, sign in again.'
										: ''
	);
	/**
	 * The manual setup is the fallback: offered when no client is available,
	 * and for the owner's own client only once something went wrong (a new
	 * sign-in is needed or the last one failed), so it can be replaced.
	 */
	const offerManual = $derived(
		view.manual === 'needed' || relayFallback || (view.manual === 'available' && (view.again || flow.phase === 'error' || manualOpen))
	);
	const canSignIn = $derived(view.step === 'sign_in' && !mobileApp && returnOk && !access.readOnly);
	$effect(() => { if (view.manual === 'none' && !relayFallback) manualOpen = false; });

	async function doSignOut() {
		error = '';
		try { await signOut(entry.plugin_id || pluginId, entry.service); }
		catch (e) { error = getErrorMessage(e, 'Sign-out failed. Try again.'); }
		await onChanged();
	}

	async function removeClient() {
		error = '';
		try { await deleteClient(entry.plugin_id || pluginId, entry.service); manualOpen = false; }
		catch (e) { error = getErrorMessage(e, 'The client could not be removed.'); }
		await onChanged();
	}

	async function saved() {
		manualOpen = false;
		await onChanged();
	}
</script>

{#snippet below()}
	{#if note}<p class="psr-note" class:err={flow.phase === 'error' || !!error} role={flow.phase === 'error' || error ? 'alert' : 'status'}>{note}</p>{/if}
	{#if via}<p class="psr-note psr-via">{relayLine(via)}</p>{/if}
	{#if manualOpen}
		<div class="psr-manual">
			{#if view.manual === 'available' && entry.client_id}
				<div class="psr-saved">
					<span class="psr-client"><span>Saved client <code>{entry.client_id}</code></span><small>{ownerClientFallback(entry)}</small></span>
					<InlineConfirm label="Remove" question="Remove client and sign-in?" confirmLabel="Remove" variant="ghost" size="sm" onConfirm={removeClient} />
				</div>
			{/if}
			<ClientSetupForm plugin={entry.plugin_id || pluginId} service={entry.service} {redirectUri}
				currentClientId={view.manual === 'available' ? entry.client_id : ''} ownApp={blocked || relayFallback}
				disabled={access.readOnly} onSaved={saved} />
		</div>
	{/if}
{/snippet}

<div class="psr" data-testid="plugin-service-{entry.service}" data-step={view.step}>
	<SettingRow label={entry.service} {hint} expanded={note || via || manualOpen ? below : undefined}>
		<span class="psr-dot"><StatusDot {tone} /></span>
		{#if view.step !== 'invalid'}
			<span class="psr-access">{accessText(entry.access)}</span>
		{/if}
		{#if offerManual && flow.phase !== 'waiting'}
			<button type="button" class="st-btn sm" class:ghost={view.manual === 'available'} aria-expanded={manualOpen}
				onclick={() => (manualOpen = !manualOpen)}>{manualOpen ? 'Hide setup' : view.manual === 'available' ? 'Change client' : 'Set up manually'}</button>
		{/if}
		{#if flow.phase === 'waiting'}
			<button type="button" class="st-btn sm ghost" onclick={() => flow.cancel()}>Stop</button>
			<button type="button" class="st-btn sm primary" disabled aria-busy="true">Waiting…</button>
		{:else if view.step === 'signed_in'}
			<InlineConfirm label="Sign out" question="Sign out of {entry.service}?" confirmLabel="Sign out" variant="ghost" size="sm" onConfirm={doSignOut} />
		{:else if view.step === 'sign_in'}
			<button type="button" class="st-btn sm primary" disabled={!canSignIn}
				aria-label={host ? `Sign in with ${host}` : 'Sign in'} onclick={() => flow.begin()}>{view.again ? 'Sign in again' : 'Sign in'}</button>
		{/if}
	</SettingRow>
</div>

<style>
	.psr { display: contents; }
	.psr-dot { display: inline-flex; align-items: center; }
	.psr-access {
		font-size: 11.5px;
		color: var(--st-mfg);
		padding: 2px 7px;
		border: 1px solid var(--st-border);
		border-radius: 999px;
		white-space: nowrap;
	}
	.psr-note {
		margin: 0 0 8px;
		font-size: 12.5px;
		color: var(--st-mfg);
	}
	.psr-note.err { color: var(--st-destructive); }
	.psr-note + .psr-via { margin-top: -4px; }
	.psr-manual { display: grid; gap: 10px; }
	.psr-saved {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		font-size: 12.5px;
		color: var(--st-mfg);
	}
	.psr-client { display: grid; gap: 2px; min-width: 0; }
	.psr-saved small { font-size: 11.5px; color: var(--st-mfg); }
	.psr-saved code {
		font-family: var(--st-mono);
		font-size: 12px;
		color: var(--st-fg);
		overflow-wrap: anywhere;
	}
</style>
