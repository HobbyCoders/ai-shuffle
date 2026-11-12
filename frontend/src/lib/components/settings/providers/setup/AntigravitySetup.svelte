<script lang="ts">
	/**
	 * AntigravitySetup: a new Google Antigravity provider row.
	 *  - Gemini API key (`agy-key`): Continue creates the row with the key, then
	 *    the models step picks which Gemini models the pickers offer.
	 *  - Personal Google sign-in (`agy-personal`): Google's terms warning first,
	 *    then the agent's own sign-in. The row is created when the sign-in
	 *    starts (the agent signs in per row) and deleted again if it is
	 *    cancelled. The row starts interactive-only.
	 *  - Business Google sign-in (`agy-business`): the agent offers it only
	 *    through a browser on the worker, so this explains that and offers the
	 *    Gemini API key instead.
	 * Needs the agent installed on this worker first (AntigravityInstall).
	 */
	import { onMount } from 'svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { createAntigravityProvider } from '$lib/api/antigravity';
	import { deleteModelProvider } from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import TextField from '../../rows/TextField.svelte';
	import InlineBanner from '../../rows/InlineBanner.svelte';
	import ModelCuration from '../ModelCuration.svelte';
	import AntigravityWarning from '../AntigravityWarning.svelte';
	import AntigravityInstall from './AntigravityInstall.svelte';
	import AntigravitySignIn from './AntigravitySignIn.svelte';
	import { antigravity } from '../antigravity.svelte';
	import { providerDrafts, type Draft } from '../drafts.svelte';
	import { ANTIGRAVITY_AUTH_MODE, rowItem, type AntigravityFlavor } from '../providers-model';

	interface Props {
		draft: Draft;
		onFinish: (item: string) => void;
		onCancel: () => void;
	}

	let { draft, onFinish, onCancel }: Props = $props();

	const flavor = $derived(draft.flavor as AntigravityFlavor);
	const mode = $derived(ANTIGRAVITY_AUTH_MODE[flavor]);
	const modeMeta = $derived(antigravity.meta?.auth_modes.find((m) => m.id === mode));
	const installed = $derived(!!antigravity.status?.installed);
	const blocked = $derived((antigravity.status?.workers_requiring_update ?? []).length > 0);

	let name = $state('');
	let key = $state('');
	let busy = $state(false);
	let error = $state('');

	onMount(() => void antigravity.load());

	async function createRow(apiKey?: string): Promise<string> {
		const row = await createAntigravityProvider({
			name: name.trim() || draft.name,
			auth_mode: mode,
			...(apiKey ? { api_key: apiKey } : {})
		});
		// A sign-in row is ours to delete until the sign-in succeeds.
		providerDrafts.update(draft.item, { rowId: row.id, ownsRow: !apiKey });
		return row.id;
	}

	async function saveKey() {
		const value = key.trim();
		if (!value || busy) return;
		busy = true;
		error = '';
		try {
			if (!draft.rowId) await createRow(value);
			await providers.refreshProviders();
			key = '';
			providerDrafts.update(draft.item, { step: 'models' });
		} catch (e) {
			error = getErrorMessage(e, 'Could not add the Antigravity provider');
		} finally {
			busy = false;
		}
	}

	async function prepareSignIn() {
		if (busy) return;
		busy = true;
		error = '';
		try {
			if (!draft.rowId) await createRow();
			providerDrafts.update(draft.item, { step: 'run' });
		} catch (e) {
			error = getErrorMessage(e, 'Could not add the Antigravity provider');
		} finally {
			busy = false;
		}
	}

	async function signedIn() {
		providerDrafts.update(draft.item, { ownsRow: false, step: 'models' });
		await providers.refreshProviders().catch(() => {});
	}

	async function cancel() {
		if (draft.ownsRow && draft.rowId) await deleteModelProvider(draft.rowId).catch(() => {});
		onCancel();
	}

	function useKeyInstead() {
		providerDrafts.update(draft.item, { flavor: 'agy-key', name: 'Gemini API key' });
	}
</script>

{#if draft.step === 'models' && draft.rowId}
	<ModelCuration mode="setup" providerId={draft.rowId} onDone={() => onFinish(rowItem(draft.rowId!))} />
{:else}
	<AntigravityInstall />
	{#if !antigravity.status}
		<!-- Waiting for the install status; AntigravityInstall says so. -->
	{:else if !installed}
		<p class="pv-note">Install Antigravity on this worker first. The provider runs Google's own agent.</p>
		<div class="pv-acts"><button type="button" class="st-btn ghost" onclick={cancel}>Cancel</button></div>
	{:else if flavor === 'agy-business'}
		<InlineBanner tone="info">
			Antigravity offers business sign-in only through a browser on the worker, and AI Shuffle drives it without one.
			A Gemini API key from the same Google Cloud project works here.
		</InlineBanner>
		<div class="pv-acts">
			<button type="button" class="st-btn ghost" onclick={cancel}>Cancel</button>
			<button type="button" class="st-btn primary" onclick={useKeyInstead}>Use a Gemini API key</button>
		</div>
	{:else if flavor === 'agy-key'}
		<p class="pv-note">Google's Antigravity agent runs on this worker with your Gemini API key. The key becomes active; it is stored encrypted and only handed to the agent.</p>
		<RowGroup title="Key">
			<SettingRow label="Name" hint="Optional, to tell your accounts apart">
				<TextField bind:value={name} placeholder="e.g. Work" maxlength={120} width={240} disabled={busy} />
			</SettingRow>
			<SettingRow label="Get a key">
				<a class="pv-link" href="https://aistudio.google.com/apikey" target="_blank" rel="noopener noreferrer">Google AI Studio ↗</a>
			</SettingRow>
			<SettingRow label="Gemini API key" hint="Usually starts with AIza">
				<TextField bind:value={key} type="password" mono placeholder="AIza…" width={240} disabled={busy} label="Gemini API key" />
			</SettingRow>
		</RowGroup>
		{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
		<div class="pv-acts">
			<button type="button" class="st-btn ghost" disabled={busy} onclick={cancel}>Cancel</button>
			<button type="button" class="st-btn primary" disabled={busy || !key.trim() || blocked} onclick={saveKey}>{busy ? 'Saving' : 'Continue'}</button>
		</div>
	{:else}
		<!-- Personal sign-in: the terms warning comes before anything starts. -->
		<AntigravityWarning />
		{#if modeMeta?.guarded}
			<p class="pv-note">This provider runs only in chats you type into yourself. Automations, scheduled runs, subagents and sessions another agent starts are refused until you allow unattended use on it.</p>
		{/if}
		{#if draft.step === 'run' && draft.rowId}
			<AntigravitySignIn rowId={draft.rowId} startLabel="Start sign-in" onSignedIn={signedIn} onCancel={cancel} />
		{:else}
			<RowGroup title="Provider">
				<SettingRow label="Name" hint="Optional, to tell your accounts apart">
					<TextField bind:value={name} placeholder="e.g. Personal" maxlength={120} width={240} disabled={busy} />
				</SettingRow>
			</RowGroup>
			{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
			<div class="pv-acts">
				<button type="button" class="st-btn ghost" disabled={busy} onclick={cancel}>Cancel</button>
				<button type="button" class="st-btn" disabled={busy} onclick={useKeyInstead}>Use a Gemini API key</button>
				<button type="button" class="st-btn primary" disabled={busy || blocked} onclick={prepareSignIn}>{busy ? 'Preparing' : 'I understand, continue'}</button>
			</div>
		{/if}
	{/if}
{/if}
