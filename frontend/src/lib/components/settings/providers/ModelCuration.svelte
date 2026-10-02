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
		filterRows,
		mediaKindLabel,
		rowsForSetup,
		rowsFromFetch,
		rowsFromStored,
		type CurationRow,
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
	}

	let { mode = 'manage', row, providerId, onDone }: Props = $props();

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
		source = '';
		const stored = untrack(() => row?.models ?? []);
		const selected = new Set(rows.filter((r) => r.checked).map((r) => r.id));
		try {
			const res = await providers.fetchProviderModels(id);
			source = res.source;
			rows = mode === 'setup' ? rowsForSetup(res, selected) : rowsFromFetch(stored, res);
			hiddenMedia = res.hidden ?? [];
		} catch (e) {
			error = getErrorMessage(e, 'Could not fetch models');
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
			await updateModelProvider(id, { models: curatedEntries(rows) });
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
	{#if mode === 'setup'}<p class="pv-msg" aria-live="polite">{setupHint}</p>{/if}
	{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
	{#if rows.length > 0}
		<div class="filter">
			<TextField bind:value={filter} type="search" placeholder="Filter models" label="Filter models" width={260} />
		</div>
	{/if}
	<div class="st-card pv-card">
		{#if loading && rows.length === 0}
			<p class="pv-empty">Loading models</p>
		{:else if rows.length === 0}
			<p class="pv-empty">No models returned.</p>
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
			<button type="button" class="st-btn ghost" disabled={saving || loading} onclick={() => onDone?.()}
				>Choose later</button
			>
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
</style>
