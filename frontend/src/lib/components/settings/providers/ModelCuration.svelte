<script lang="ts">
	/**
	 * ModelCuration: which of a credential's models the pickers offer.
	 *
	 * Two uses:
	 *  - `manage` (a credential's Models tab): loads the catalog, pre-checks the
	 *    saved curation and saves every toggle on the spot. A failed fetch keeps
	 *    the saved list checked so nothing can be wiped.
	 *  - `setup` (right after a key or sign-in is added): nothing is checked;
	 *    "Use selected" saves the choice, "Choose later" leaves it empty.
	 *  - `setup` with `wizard` (the first-run setup wizard): a model is required.
	 *    There is no "Choose later". A model from the provider's built-in chat
	 *    roster (`recommended`) is pre-checked when the list has one, else
	 *    nothing is and the owner must pick. Saving also sets the row's
	 *    `default_model` when it has none (the first checked model, the one the
	 *    worker then runs), and a failed fetch says why in plain
	 *    words: a rejected key goes back to the key form (`onRejected`), a
	 *    timeout or server error offers Retry, and an endpoint with no model list
	 *    offers Remove (`onRemove`).
	 *
	 * Saved entries keep `context_window`, pricing and the reasoning metadata
	 * (see curation.ts).
	 */
	import { onMount, untrack } from 'svelte';
	import { RefreshCw } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { updateModelProvider, type ModelProvider } from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';
	import { notify } from '$lib/stores/notifications';
	import { watchResources } from '$lib/services/watch-resources';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import TextField from '../rows/TextField.svelte';
	import LinkValue from '../rows/LinkValue.svelte';
	import {
		curatedEntries,
		describeFetchFailure,
		filterRows,
		mediaKindLabel,
		precheckRecommended,
		rowsForSetup,
		rowsFromFetch,
		rowsFromStored,
		type CurationRow,
		type FetchFailure,
		type HiddenMedia
	} from './curation';

	interface Props {
		mode?: 'manage' | 'setup';
		/** Manage: the stored row. */
		row?: ModelProvider;
		/** Setup: the row the setup created. */
		providerId?: string;
		/** Setup: finished (models saved, or chosen later). */
		onDone?: () => void;
		/** Setup wizard: require a model, explain a failed fetch (see above). */
		wizard?: boolean;
		/** Wizard: the provider's name for its messages ("OpenAI"). */
		providerName?: string;
		/** Wizard: the provider refused the key (401/403). */
		onRejected?: (message: string) => void;
		/** Wizard: drop this row (an endpoint with no model list). */
		onRemove?: () => void;
	}

	let {
		mode = 'manage',
		row,
		providerId,
		onDone,
		wizard = false,
		providerName = 'The provider',
		onRejected,
		onRemove
	}: Props = $props();
	let failure = $state<FetchFailure | null>(null);

	const id = $derived(row?.id ?? providerId ?? '');

	let rows = $state<CurationRow[]>([]);
	let loading = $state(false);
	let saving = $state(false);
	let error = $state('');
	let filter = $state('');
	let source = $state<'preset' | 'upstream' | ''>('');
	let hiddenMedia = $state<HiddenMedia[]>([]);

	const checked = $derived(rows.filter((r) => r.checked).length);
	const shown = $derived(filterRows(rows, filter));
	const hiddenMatch = $derived.by(() => {
		const q = filter.trim().toLowerCase();
		if (!q) return null;
		return hiddenMedia.find((m) => m.id.toLowerCase().includes(q)) ?? null;
	});

	async function load() {
		if (!id || loading) return;
		loading = true;
		error = '';
		failure = null;
		source = '';
		const stored = untrack(() => row?.models ?? []);
		const selected = new Set(rows.filter((r) => r.checked).map((r) => r.id));
		try {
			const res = await providers.fetchProviderModels(id);
			source = res.source;
			rows = mode === 'setup' ? rowsForSetup(res, selected) : rowsFromFetch(stored, res);
			// The wizard needs a model: pre-check a known chat model, if any.
			if (wizard) rows = precheckRecommended(rows, res.recommended);
			hiddenMedia = res.hidden ?? [];
		} catch (e) {
			const detail = getErrorMessage(e, 'Could not fetch models');
			if (wizard) {
				failure = describeFetchFailure(detail, providerName);
				if (failure.kind === 'rejected') onRejected?.(failure.message);
			} else {
				error = detail;
			}
			if (mode === 'manage') rows = rowsFromStored(stored);
		} finally {
			loading = false;
		}
	}

	async function toggle(modelId: string) {
		const next = rows.map((r) => (r.id === modelId ? { ...r, checked: !r.checked } : r));
		if (mode === 'setup') {
			rows = next;
			return;
		}
		if (!id || saving) return;
		const previous = rows;
		rows = next;
		saving = true;
		try {
			await updateModelProvider(id, { models: curatedEntries(next) });
			await providers.refreshProviders();
			flashSaved();
		} catch (e) {
			rows = previous;
			notify(getErrorMessage(e, 'Could not save models'), { severity: 'error' });
		} finally {
			saving = false;
		}
	}

	async function useSelected() {
		if (!id || saving || loading || checked === 0) return;
		saving = true;
		error = '';
		try {
			const models = curatedEntries(rows);
			// The wizard also names the row's default (the first picked model)
			// when it has none, or when its default is no longer picked, so the
			// worker and this UI agree on what runs.
			const current = untrack(() => providers.current.providers?.find((p) => p.id === id));
			const keepDefault = !!current?.default_model && models.some((m) => m.id === current.default_model);
			const setDefault = wizard && models.length > 0 && !keepDefault;
			await updateModelProvider(id, { models, ...(setDefault ? { default_model: models[0].id } : {}) });
			await providers.refreshProviders();
			onDone?.();
		} catch (e) {
			error = getErrorMessage(e, 'Could not save models');
		} finally {
			saving = false;
		}
	}

	onMount(() => {
		void load();
	});

	watchResources(['settings'], async () => {
		if (mode === 'manage' && !saving) await load();
	});

	const title = $derived(mode === 'setup' ? 'Models' : 'Offered in pickers');
	const count = $derived(rows.length ? `${checked} of ${rows.length}` : undefined);
	const setupHint = $derived(
		loading
			? 'Fetching models'
			: source === 'preset'
				? 'Preset catalog, pick what to offer'
				: `Fetched ${rows.length} ${rows.length === 1 ? 'model' : 'models'}, pick what to offer`
	);
</script>

<RowGroup {title} {count} bare>
	{#snippet actions()}
		<button
			type="button"
			class="st-btn icon ghost sm"
			aria-label="Refresh model list"
			title="Refresh model list"
			disabled={loading || saving}
			onclick={load}
		>
			<RefreshCw />
		</button>
	{/snippet}
	{#if mode === 'setup' && !failure}<p class="pv-msg" aria-live="polite">{setupHint}</p>{/if}
	{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
	{#if failure}
		<div class="pv-fail" role="alert" data-testid="curation-failure" data-kind={failure.kind}>
			<p class="pv-msg err">{failure.message}</p>
			{#if failure.kind !== 'rejected' || !onRejected}
				<div class="pv-fail-acts">
					{#if failure.kind === 'no-list' && onRemove}
						<button type="button" class="st-btn ghost" onclick={() => onRemove?.()}>Remove</button>
					{/if}
					<button type="button" class="st-btn primary" disabled={loading} onclick={load}>
						{loading ? 'Retrying' : 'Retry'}
					</button>
				</div>
			{/if}
		</div>
	{/if}
	{#if rows.length > 0}
		<div class="filter">
			<TextField bind:value={filter} type="search" placeholder="Filter models" label="Filter models" width={260} />
		</div>
	{/if}
	<div class="st-card pv-card">
		{#if loading && rows.length === 0}
			<p class="pv-empty">Loading models</p>
		{:else if rows.length === 0}
			<p class="pv-empty">{failure ? 'No models yet.' : 'No models returned.'}</p>
		{:else}
			<div class="pv-mlist">
				{#each shown as r (r.id)}
					<label class="pv-mrow">
						<input type="checkbox" class="pv-chk" checked={r.checked} disabled={saving} onchange={() => toggle(r.id)} />
						<span class="id">{r.id}</span>
						{#if r.label && r.label !== r.id}<span class="lbl">{r.label}</span>{:else}<span></span>{/if}
					</label>
				{/each}
				{#if shown.length === 0 && !hiddenMatch}
					<p class="pv-empty">No models match “{filter}”.</p>
				{/if}
			</div>
			{#if hiddenMatch}
				<!-- "No matches" would be wrong: the key offers this model, it is set elsewhere. -->
				<SettingRow label="{hiddenMatch.id} is a {mediaKindLabel(hiddenMatch.media_kind)} model">
					{#if hiddenMatch.media_kind === 'voice'}
						<LinkValue label="Voice" target={{ category: 'voice' }} />
					{:else}
						<LinkValue label="Default models" target={{ category: 'models' }} />
					{/if}
				</SettingRow>
			{/if}
		{/if}
	</div>
	{#if mode === 'setup'}
		<div class="pv-acts">
			{#if !wizard}
				<button type="button" class="st-btn ghost" disabled={saving || loading} onclick={() => onDone?.()}
					>Choose later</button
				>
			{/if}
			{#if wizard && !loading && rows.length > 0 && checked === 0}
				<p class="pv-msg" data-testid="curation-pick-hint">Pick a model to continue.</p>
			{/if}
			<button type="button" class="st-btn primary" disabled={saving || loading || checked === 0} onclick={useSelected}>
				{saving ? 'Saving' : `Use selected${checked ? ` (${checked})` : ''}`}
			</button>
		</div>
	{/if}
</RowGroup>

<style>
	.filter {
		margin: 0 0 8px;
	}
	.filter :global(.st-txt) {
		width: 100% !important;
	}
	.pv-fail {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 8px 12px;
		margin: 0 0 10px;
	}
	.pv-fail .pv-msg {
		flex: 1 1 220px;
		margin: 0;
	}
	.pv-fail-acts {
		display: flex;
		gap: 8px;
		margin-left: auto;
	}
</style>
