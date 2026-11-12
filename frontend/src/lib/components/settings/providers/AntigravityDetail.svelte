<script lang="ts">
	import { detailLine } from './credential-display';
	/**
	 * A Google Antigravity provider row (Google's own agent on the worker).
	 * Overview: what the agent reports for this row (signed in, signed out, not
	 * installed, error), sign-in and sign-out for Google sign-in rows, the key
	 * for key rows, and the interactive-only guard on personal sign-ins with
	 * Google's terms warning. Models: curation (Gemini models only). Usage: the
	 * agent's own /usage reading, read on demand.
	 */
	import { onMount, untrack } from 'svelte';
	import { ExternalLink, RefreshCw } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		getAntigravityProviderStatus,
		setAntigravityGuard,
		signOutAntigravity,
		type AntigravityProviderStatus
	} from '$lib/api/antigravity';
	import { updateModelProvider } from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';
	import { windowLabel } from '$lib/stores/rate-limit';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { DangerAction } from '../shell/DangerZone.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import TextField from '../rows/TextField.svelte';
	import Switch from '../rows/Switch.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import ModelCuration from './ModelCuration.svelte';
	import SavedSecretRow from './SavedSecretRow.svelte';
	import CredentialControl from './CredentialControl.svelte';
	import AntigravityWarning from './AntigravityWarning.svelte';
	import AntigravityInstall from './setup/AntigravityInstall.svelte';
	import AntigravitySignIn from './setup/AntigravitySignIn.svelte';
	import { antigravity } from './antigravity.svelte';
	import { antigravityGuarded, type CredentialView, type ProviderView } from './providers-model';

	interface Props {
		credential: CredentialView;
		provider: ProviderView;
	}

	let { credential, provider }: Props = $props();

	const row = $derived(credential.row!);
	const keyRow = $derived(credential.flavor === 'agy-key');
	const personal = $derived(credential.flavor === 'agy-personal');
	const business = $derived(credential.flavor === 'agy-business');

	let status = $state<AntigravityProviderStatus | null>(null);
	let statusError = $state('');
	let signingIn = $state(false);
	let saving = $state(false);
	let usageBusy = $state(false);
	let error = $state('');
	let notice = $state('');

	// Guard: the row's own setting wins until the worker's view loads.
	const guarded = $derived(status ? status.guarded : antigravityGuarded(row));

	async function loadStatus() {
		const id = row.id;
		try {
			const answer = await getAntigravityProviderStatus(id);
			if (id !== row.id) return;
			status = answer;
			statusError = '';
			// The answer moves the row's "Needs sign-in" state, readiness and
			// the picker: reload the list when it changed.
			const signedIn = answer.state === 'signed_in' ? true : answer.state === 'signed_out' ? false : null;
			if (!keyRow && signedIn !== null && (row.antigravity_signed_in ?? null) !== signedIn) {
				await providers.refreshProviders();
			}
		} catch (e) {
			if (id === row.id) statusError = getErrorMessage(e, 'Could not ask the agent');
		}
	}

	async function save(body: Parameters<typeof updateModelProvider>[1]): Promise<boolean> {
		if (saving) return false;
		saving = true;
		error = '';
		try {
			await updateModelProvider(row.id, body);
			await providers.refreshProviders();
			flashSaved();
			return true;
		} catch (e) {
			error = getErrorMessage(e, 'Could not save');
			return false;
		} finally {
			saving = false;
		}
	}

	function rename(name: string) {
		if (name.trim() && name.trim() !== row.name) void save({ name: name.trim() });
	}

	async function setUnattended(allow: boolean) {
		error = '';
		try {
			const view = await setAntigravityGuard(row.id, allow);
			if (status) status = { ...status, ...view };
			await providers.refreshProviders();
			flashSaved();
		} catch (e) {
			error = getErrorMessage(e, 'Could not change unattended use');
			throw e;
		}
	}

	async function signOut() {
		error = '';
		try {
			await signOutAntigravity(row.id);
			notice = '';
			await loadStatus();
			flashSaved();
		} catch (e) {
			error = getErrorMessage(e, 'Could not sign out');
		}
	}

	// The forced read answers before the agent has replied; follow the pass
	// (as the composer's usage popover does) until it lands.
	let destroyed = false;
	async function refreshUsage() {
		usageBusy = true;
		try {
			await providers.loadUsage(true);
			const deadline = Date.now() + 30_000;
			while (!destroyed && providers.current.usage?.provider_refreshing && Date.now() < deadline) {
				await new Promise((r) => setTimeout(r, 1_000));
				await providers.loadUsage();
			}
		} finally {
			usageBusy = false;
		}
	}

	const usageEntry = $derived(providers.current.usage?.providers?.find((p) => p.provider_id === row.id) ?? null);
	const usageWindows = $derived(usageEntry?.windows ?? []);
	const usageState = $derived(
		usageEntry?.last_error_kind === 'unavailable' || usageEntry?.last_error_kind === 'timeout' || usageEntry?.status === 'unavailable'
			? 'unavailable'
			: !usageEntry?.last_success_at
				? 'never'
				: usageEntry.status === 'stale'
					? 'stale'
					: 'fresh'
	);
	const dashboard = $derived(usageEntry?.source_info?.dashboard_url || antigravity.meta?.dashboard_url || null);

	const stateText = $derived.by(() => {
		if (!status) return statusError ? 'Unknown' : 'Checking';
		switch (status.state) {
			case 'signed_in': return keyRow ? 'Key accepted by the agent' : 'Signed in';
			case 'signed_out': return keyRow ? 'Key not accepted' : 'Not signed in';
			case 'not_installed': return 'Antigravity not installed';
			default: return 'Agent error';
		}
	});
	const stateTone = $derived(!status ? undefined : status.state === 'signed_in' ? 'ok' : status.state === 'error' ? 'err' : 'warn');

	const position = $derived(
		!row.enabled
			? 'Not in the model picker'
			: status?.state === 'not_installed'
				? 'Install Antigravity to use it'
				: status && status.state !== 'signed_in'
					? keyRow ? 'Check the key' : 'Sign in to use it'
				: `${row.models.length} models in picker`
	);
	const line = $derived(detailLine(position, personal ? (guarded ? 'Interactive only' : 'Unattended use allowed') : null));

	const tabs = [{ id: 'Overview' }, { id: 'Models' }, { id: 'Usage' }];

	const danger = $derived<DangerAction[]>([
		{
			label: 'Remove this credential',
			hint: keyRow ? 'Deletes the stored key' : 'Signs the agent out and removes this provider',
			button: 'Remove',
			question: `Remove ${credential.name}?`,
			onConfirm: async () => {
				if (!keyRow) await signOutAntigravity(row.id).catch(() => {});
				await providers.deleteProvider(row.id);
				settingsNav.selectItem(provider.item, 'providers');
			}
		}
	]);

	// The agent can be installed, signed in or out, or removed outside this
	// screen (another device, another tab, a terminal on the worker): ask
	// again whenever the pane is shown, including a reopen on a phone, where
	// the pane stays mounted behind the list, and when the app regains focus.
	function refresh() {
		notice = '';
		void antigravity.load(true);
		void loadStatus();
		void providers.loadUsage();
	}
	let shownFor = '';
	let statusFor = '';
	$effect(() => {
		const shown = !settingsNav.stacked || settingsNav.view === 'detail';
		const id = row.id;
		untrack(() => {
			if (!shown) {
				shownFor = '';
				return;
			}
			if (shownFor === id) return;
			if (statusFor !== id) status = null; // another row's answer never shows here
			statusFor = shownFor = id;
			refresh();
		});
	});
	onMount(() => {
		const onFocus = () => {
			if (document.visibilityState === 'visible') void loadStatus();
		};
		window.addEventListener('focus', onFocus);
		document.addEventListener('visibilitychange', onFocus);
		return () => {
			destroyed = true;
			window.removeEventListener('focus', onFocus);
			document.removeEventListener('visibilitychange', onFocus);
		};
	});
</script>

<DetailPane
	name={credential.name}
	badge={credential.kind}
	status={status && status.state !== 'signed_in' ? (stateTone as 'warn' | 'err') : undefined}
	statusText={status && status.state !== 'signed_in' ? stateText : undefined}
	subtitle={line}
	{tabs}
	{danger}
	item={credential.item}
>
	{#snippet tile()}<ProviderTile {provider} size="lg" />{/snippet}
	{#snippet primary()}
		{#if !keyRow && status?.state === 'signed_out' && !signingIn && !business}
			<button type="button" class="st-btn warn" onclick={() => { settingsNav.setTab('providers', credential.item, 'Overview'); signingIn = true; }}><RefreshCw />Sign in</button>
		{/if}
		<CredentialControl {credential} showState />
	{/snippet}
	{#snippet children(tab: string | null)}
		{#if tab === 'Models'}
			{#key row.id}<ModelCuration {row} />{/key}
		{:else if tab === 'Usage'}
			<RowGroup title="Quota" id="providers-antigravity-usage">
				{#if usageWindows.length}
					{#each usageWindows as w (w.type)}
						<SettingRow label={windowLabel(w.type)} hint={usageState === 'fresh' ? undefined : 'Last reading'}>
							<ValueText tone="fg" value={`${Math.max(0, Math.round(100 - w.utilization))}% left`} />
						</SettingRow>
					{/each}
				{/if}
				<SettingRow
					label="Reading"
					hint="From the agent's own /usage, only when you refresh"
					help="Google documents no usage API for Antigravity. AI Shuffle asks the agent on this worker when you refresh, at most every 30 seconds, and shows unavailable when its reply cannot be read."
				>
					<ValueText
						status={usageState === 'fresh' ? 'ok' : usageState === 'never' ? undefined : 'warn'}
						value={usageState === 'unavailable' ? 'Unavailable' : usageState === 'never' ? 'Not read yet' : usageState === 'stale' ? 'Old reading' : 'Current'}
					/>
				</SettingRow>
			</RowGroup>
			<div class="pv-acts">
				{#if dashboard}
					<a class="st-btn ghost" href={dashboard} target="_blank" rel="noopener noreferrer"><ExternalLink />{usageEntry?.source_info?.dashboard_label || 'Antigravity FAQ'}</a>
				{/if}
				<button type="button" class="st-btn" disabled={usageBusy} onclick={refreshUsage}><RefreshCw />{usageBusy ? 'Reading' : 'Refresh'}</button>
			</div>
		{:else}
			{#if personal}<AntigravityWarning />{/if}
			{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
			{#if notice}<p class="pv-note" role="status">{notice}</p>{/if}
			<RowGroup title="Account">
				<SettingRow label="Name"><TextField value={row.name} width={220} maxlength={120} disabled={saving} onchange={rename} /></SettingRow>
				<SettingRow label="Connection" hint={keyRow ? undefined : 'The agent keeps the sign-in on this worker; AI Shuffle never reads it'}>
					<ValueText value={credential.kind === 'API key' ? 'Gemini API key' : personal ? 'Personal Google sign-in' : 'Business Google sign-in'} />
				</SettingRow>
				<SettingRow label="Status" hint={status?.message ?? undefined}>
					<ValueText status={stateTone as 'ok' | 'warn' | 'err' | undefined} value={stateText} />
				</SettingRow>
				{#if keyRow}
					<SavedSecretRow
						label="Gemini API key"
						saved={row.auth_token_set}
						placeholder="AIza…"
						busy={saving}
						onsave={(value) => save({ auth_token: value }).then(async (ok) => { if (ok) await loadStatus(); return ok; })}
					/>
				{:else if status?.state === 'signed_in'}
					<SettingRow label="Google account" hint="Signs the agent out on this worker">
						<button type="button" class="st-btn" onclick={signOut}>Sign out</button>
					</SettingRow>
				{/if}
			</RowGroup>
			{#if statusError}<p class="pv-msg err" role="alert">{statusError}</p>{/if}

			{#if !keyRow && (signingIn || status?.state === 'signed_out' || status?.login)}
				{#if business}
					<p class="pv-note">Antigravity offers business sign-in only through a browser on the worker. Sign in there with <code>agy</code>, or use a Gemini API key.</p>
				{:else}
					<AntigravitySignIn
						rowId={row.id}
						pending={status?.login ?? null}
						onSignedIn={(note) => { signingIn = false; notice = note ?? ''; void loadStatus(); if (!note) flashSaved(); }}
						onCancel={signingIn ? () => (signingIn = false) : undefined}
					/>
				{/if}
			{/if}

			{#if personal}
				<RowGroup title="Interactive only" id="providers-antigravity-guard">
					<SettingRow
						label="Allow unattended use"
						hint={guarded ? 'Off: runs only in chats you type into' : 'On: automations and subagents can use it'}
						help="A personal Google sign-in runs only in chats you type into yourself. Automations, scheduled or automatic turns, Task, Swarm and subagent children, sessions another agent started and background calls are refused while this is off."
					>
						<Switch checked={!guarded} controlled onchange={(v) => setUnattended(v)} label="Allow unattended use" />
					</SettingRow>
				</RowGroup>
			{/if}

			<AntigravityInstall />
		{/if}
	{/snippet}
</DetailPane>
