<script lang="ts">
	/**
	 * Claude subscription > Models: curate which models this worker's Claude Code
	 * CLI offers in model pickers. Matches ModelCuration.svelte's manage UX.
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

	type SubscriptionModel = { id: string; label: string; enabled: boolean };
	type SubscriptionCatalogResponse = {
		models: SubscriptionModel[];
		captured: boolean;
		cli_version?: string | null;
		captured_at?: string | null;
	};
	type Row = { id: string; label: string; checked: boolean };

	interface Props {
		/** Viewer without settings access: the list shows, changes and Refresh do not run. */
		readOnly?: boolean;
		/** Refresh asks Claude Code CLI; available only in subscription mode. */
		canRefresh?: boolean;
	}

	let { readOnly = false, canRefresh = true }: Props = $props();
	const providerDescriptionId = $props.id();

	let rows = $state<Row[]>([]);
	let captured = $state(false);
	let cliVersion = $state<string | null>(null);
	let capturedAt = $state<string | null>(null);
	let loading = $state(true);
	let refreshing = $state(false);
	let saving = $state(false);
	let error = $state('');
	let filter = $state('');

	let request: AbortController | undefined;
	let reloadPending = false;

	const checked = $derived(rows.filter((r) => r.checked).length);
	const shown = $derived.by(() => {
		const q = filter.trim().toLowerCase();
		if (!q) return rows;
		return rows.filter((r) => r.id.toLowerCase().includes(q) || r.label.toLowerCase().includes(q));
	});
	const count = $derived(rows.length ? `${checked} of ${rows.length}` : undefined);

	async function load() {
		if (request || saving) {
			reloadPending = true;
			return;
		}
		const controller = new AbortController();
		request = controller;
		loading = true;
		error = '';
		try {
			const res = await api.get<SubscriptionCatalogResponse>('/models/claude/subscription', {
				signal: controller.signal
			});
			if (controller.signal.aborted) return;
			rows = (res.models ?? []).map((m) => ({
				id: m.id,
				label: m.label || m.id,
				checked: m.enabled !== false
			}));
			captured = !!res.captured;
			cliVersion = res.cli_version ?? null;
			capturedAt = res.captured_at ?? null;
		} catch (err) {
			if (controller.signal.aborted) return;
			error = getErrorMessage(err, 'Could not load Claude models.');
		} finally {
			if (request === controller) request = undefined;
			if (!controller.signal.aborted) {
				loading = false;
				if (reloadPending) {
					reloadPending = false;
					void load();
				}
			}
		}
	}

	async function refresh() {
		if (refreshing || saving || readOnly || !canRefresh) return;
		refreshing = true;
		error = '';
		try {
			await api.post('/models/claude/refresh', {});
			await load();
			await hydrateModelRegistry();
		} catch (e) {
			error = getErrorMessage(e, 'Could not ask Claude Code for its models.');
		} finally {
			refreshing = false;
		}
	}

	async function toggle(modelId: string) {
		if (saving || readOnly) return;
		const previousRows = rows;
		const current = rows.find((r) => r.id === modelId);
		if (!current) return;
		const nextEnabled = !current.checked;
		rows = rows.map((r) => (r.id === modelId ? { ...r, checked: nextEnabled } : r));
		saving = true;
		try {
			await api.put('/models/claude/subscription/models', {
				model: modelId,
				enabled: nextEnabled
			});
			await hydrateModelRegistry();
			flashSaved();
		} catch (err) {
			rows = previousRows;
			notify(getErrorMessage(err, 'Could not save models'), { severity: 'error' });
		} finally {
			saving = false;
			if (reloadPending) {
				reloadPending = false;
				void load();
			}
		}
	}

	onMount(() => {
		void load();
		return () => request?.abort();
	});

	watchResources(['settings'], () => {
		void load();
	});
</script>

<RowGroup title="Offered in pickers" {count} bare>
	{#snippet actions()}
		{#if canRefresh}
			{#if refreshing || loading}
				<span class="fetching" role="status" data-testid="curation-fetching">Fetching list</span>
			{/if}
			<button
				type="button"
				class="st-btn icon ghost sm"
				class:spin={refreshing}
				aria-label="Refresh model list"
				aria-busy={refreshing}
				title={refreshing ? 'Fetching the model list' : 'Refresh model list'}
				disabled={refreshing || saving || readOnly}
				onclick={refresh}
			>
				<RefreshCw />
			</button>
		{/if}
	{/snippet}
	{#if captured && capturedAt}
		<p class="pv-msg listed">
			Last refreshed <time datetime={capturedAt}>{new Date(capturedAt).toLocaleString()}</time>{cliVersion ? ` · Claude Code ${cliVersion}` : ''}
		</p>
	{:else if captured}
		<p class="pv-msg listed">Models Claude Code offers on this worker.</p>
	{:else if !loading}
		<p class="pv-msg listed">Claude Code has not listed its models on this worker yet. Refresh asks it, without sending a prompt.</p>
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
			<p class="pv-empty">No models reported yet.</p>
		{:else}
			<div class="pv-mlist" data-testid="claude-cli-models">
				<span id={providerDescriptionId} hidden>Claude</span>
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
		animation: claude-cli-spin 1s linear infinite;
	}
	@keyframes claude-cli-spin {
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
