<script lang="ts">
	/**
	 * Claude API key > Models: curate which Anthropic models this saved key
	 * offers in model pickers. Matches ModelCuration.svelte's manage UX.
	 */
	import { onMount } from 'svelte';
	import { RefreshCw } from 'lucide-svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { hydrateModelRegistry } from '$lib/constants/models.svelte';
	import { watchResources } from '$lib/services/watch-resources';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { notify } from '$lib/stores/notifications';
	import Checkbox from '$lib/components/ui/Checkbox.svelte';
	import ProviderGlyph from '../../ProviderGlyph.svelte';
	import EmptyDetail from '../../shell/EmptyDetail.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import TextField from '../../rows/TextField.svelte';

	type ApiModel = { id: string; label: string };
	type ApiCatalogResponse = {
		models: ApiModel[];
		curated?: ApiModel[] | null;
		fetched_at?: string | null;
	};
	type Row = ApiModel & { checked: boolean };

	interface Props {
		accountId: string;
		readOnly?: boolean;
	}

	let { accountId, readOnly = false }: Props = $props();
	const providerDescriptionId = $props.id();

	let rows = $state<Row[]>([]);
	let savedCurated = $state<ApiModel[] | null>(null);
	let fetchedAt = $state<string | null>(null);
	let loading = $state(true);
	let saving = $state(false);
	let error = $state('');
	let filter = $state('');

	let request: AbortController | undefined;
	let reloadPending = false;

	function buildRows(catalog: ApiModel[], curated: ApiModel[] | null): Row[] {
		if (curated === null) {
			return catalog.map((m) => ({ id: m.id, label: m.label || m.id, checked: true }));
		}
		const catalogIds = new Set(catalog.map((m) => m.id));
		const curatedIds = new Set(curated.map((m) => m.id));
		const extras: Row[] = curated
			.filter((m) => !catalogIds.has(m.id))
			.map((m) => ({ id: m.id, label: m.label || m.id, checked: true }));
		const listed: Row[] = catalog.map((m) => ({
			id: m.id,
			label: m.label || m.id,
			checked: curatedIds.has(m.id)
		}));
		return [...extras, ...listed];
	}

	const checked = $derived(rows.filter((r) => r.checked).length);
	const shown = $derived.by(() => {
		const q = filter.trim().toLowerCase();
		if (!q) return rows;
		return rows.filter((r) => r.id.toLowerCase().includes(q) || r.label.toLowerCase().includes(q));
	});
	const count = $derived(rows.length ? `${checked} of ${rows.length}` : undefined);

	async function load(refresh = false) {
		if (!accountId) return;
		if (request && !refresh) {
			reloadPending = true;
			return;
		}
		if (saving) {
			reloadPending = true;
			return;
		}
		request?.abort();
		const controller = new AbortController();
		request = controller;
		loading = true;
		error = '';
		try {
			const suffix = refresh ? '?refresh=true' : '';
			const result = await api.get<ApiCatalogResponse>(
				`/models/claude/api-key/${encodeURIComponent(accountId)}${suffix}`,
				{ signal: controller.signal }
			);
			if (controller.signal.aborted) return;
			const nextCurated = result.curated === undefined ? null : result.curated;
			savedCurated = nextCurated;
			fetchedAt = result.fetched_at ?? null;
			rows = buildRows(result.models ?? [], nextCurated);
		} catch (err) {
			if (controller.signal.aborted) return;
			error = getErrorMessage(err, 'Could not load Anthropic models.');
			if (savedCurated !== null) {
				rows = savedCurated.map((m) => ({ id: m.id, label: m.label || m.id, checked: true }));
			} else {
				rows = [];
				fetchedAt = null;
			}
		} finally {
			if (request === controller) request = undefined;
			if (!controller.signal.aborted) {
				loading = false;
				if (reloadPending) {
					reloadPending = false;
					void load(false);
				}
			}
		}
	}

	async function toggle(modelId: string) {
		if (saving || readOnly || !accountId) return;
		const previousRows = rows;
		const previousCurated = savedCurated;
		const next = rows.map((r) => (r.id === modelId ? { ...r, checked: !r.checked } : r));
		const payload = next
			.filter((r) => r.checked)
			.map(({ id, label }) => ({ id, label: label || id }));
		rows = next;
		saving = true;
		try {
			const res = await api.put<{ models: ApiModel[] }>(
				`/models/claude/api-key/${encodeURIComponent(accountId)}/models`,
				{ models: payload }
			);
			savedCurated = res.models;
			await hydrateModelRegistry();
			flashSaved();
		} catch (err) {
			rows = previousRows;
			savedCurated = previousCurated;
			notify(getErrorMessage(err, 'Could not save models'), { severity: 'error' });
		} finally {
			saving = false;
			if (reloadPending) {
				reloadPending = false;
				void load(false);
			}
		}
	}

	onMount(() => {
		void load(false);
		return () => request?.abort();
	});

	watchResources(['settings'], () => {
		void load(false);
	});
</script>

<RowGroup title="Offered in pickers" {count} bare>
	{#snippet actions()}
		{#if loading}
			<span class="fetching" role="status" data-testid="curation-fetching">Fetching list</span>
		{/if}
		<button
			type="button"
			class="st-btn icon ghost sm"
			class:spin={loading}
			aria-label="Refresh model list"
			aria-busy={loading}
			title={loading ? 'Fetching the model list' : 'Refresh model list'}
			disabled={loading || saving || readOnly}
			onclick={() => load(true)}
		>
			<RefreshCw />
		</button>
	{/snippet}
	{#if fetchedAt}
		<p class="pv-msg listed">
			Last refreshed <time datetime={fetchedAt}>{new Date(fetchedAt).toLocaleString()}</time>
		</p>
	{:else if loading && rows.length === 0}
		<p class="pv-msg listed" data-testid="curation-list-pending">Fetching the model list.</p>
	{/if}
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
			<p class="pv-empty">{error ? 'No models yet.' : 'Anthropic did not list any models for this key.'}</p>
		{:else}
			<div class="pv-mlist" data-testid="claude-api-models">
				<span id={providerDescriptionId} hidden>Claude API</span>
				{#each shown as r (r.id)}
					<label class="pv-mrow">
						<Checkbox
							checked={r.checked}
							disabled={saving || readOnly}
							aria-describedby={providerDescriptionId}
							onchange={() => toggle(r.id)}
						/>
						<span class="id curated-identity">
							<ProviderGlyph kind="claude" />
							<span>{r.id}</span>
						</span>
						{#if r.label && r.label !== r.id}
							<span class="lbl">{r.label}</span>
						{:else}
							<span></span>
						{/if}
					</label>
				{/each}
				{#if shown.length === 0}
					<EmptyDetail message={`No models match “${filter}”.`} actionLabel="Clear" onAction={() => (filter = '')} />
				{/if}
			</div>
		{/if}
	</div>
</RowGroup>

<style>
	.filter {
		margin: 0 0 8px;
	}
	.fetching {
		font-size: 12px;
		color: var(--st-mfg);
	}
	.spin :global(svg) {
		animation: claude-api-spin 1s linear infinite;
	}
	@keyframes claude-api-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.spin :global(svg) {
			animation: none;
		}
	}
	.listed {
		margin-bottom: 8px;
	}
	.curated-identity {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.curated-identity :global(.provider-glyph) {
		flex: none;
	}
	.curated-identity > span {
		min-width: 0;
		overflow-wrap: anywhere;
	}
	:global(.settings-shell) .pv-mrow {
		grid-template-columns: auto minmax(0, 1fr) fit-content(45%);
	}
	:global(.settings-shell) .pv-mrow > .id,
	:global(.settings-shell) .pv-mrow > .lbl {
		white-space: normal;
		overflow-wrap: anywhere;
	}
	:global(.settings-shell) .pv-mrow > .lbl {
		max-width: none;
	}
</style>
