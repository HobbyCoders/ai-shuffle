<script lang="ts">
	import { RefreshCw } from 'lucide-svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import RowGroup from '../../rows/RowGroup.svelte';

	type ApiModel = { id: string; label: string };
	let { accountId }: { accountId: string } = $props();
	let models = $state<ApiModel[] | null>(null);
	let loading = $state(true);
	let error = $state('');
	let request: AbortController | undefined;

	async function load(id: string) {
		request?.abort();
		const controller = new AbortController();
		request = controller;
		loading = true;
		error = '';
		try {
			const result = await api.get<{ models: ApiModel[] }>(
				`/models/claude/api-key/${encodeURIComponent(id)}`, { signal: controller.signal }
			);
			if (!controller.signal.aborted) models = result.models;
		} catch (err) {
			if (!controller.signal.aborted) {
				models = null;
				error = getErrorMessage(err, 'Could not load Anthropic models.');
			}
		} finally {
			if (!controller.signal.aborted) loading = false;
		}
	}

	$effect(() => {
		void load(accountId);
		return () => request?.abort();
	});
</script>

<RowGroup title="Models" count={models?.length} bare>
	{#snippet actions()}
		<button type="button" class="st-btn icon ghost sm" aria-label="Refresh models" title="Refresh models"
			disabled={loading} onclick={() => load(accountId)}><RefreshCw /></button>
	{/snippet}
	<p class="pv-msg">Models Anthropic makes available to this API key.</p>
	{#if loading}<p class="pv-msg" role="status">Loading Anthropic models…</p>{/if}
	{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
	{#if models}
		<div class="st-card pv-card">
			<div class="pv-mlist">
				{#each models as model (model.id)}
					<div class="pv-mrow static api-model-row">
						<span class="id">{model.id}</span>
						<span class="lbl">{model.label}</span>
					</div>
				{:else}
					<p class="pv-empty">Anthropic did not list any models for this key.</p>
				{/each}
			</div>
		</div>
	{/if}
</RowGroup>

<style>
	:global(.settings-shell) .api-model-row {
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}
	:global(.settings-shell) .api-model-row > span {
		max-width: none;
		white-space: normal;
		overflow-wrap: anywhere;
	}
</style>
