<script lang="ts">
	import { onMount } from 'svelte';
	import { Loader2, RefreshCw } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { updateModelProvider, copyReasoningMetadata, type ProviderModelEntry } from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';

	interface Props {
		providerId: string;
		onDone: () => void;
		onBusyChange?: (busy: boolean) => void;
	}
	let { providerId, onDone, onBusyChange }: Props = $props();

	// ----- Fetch-models picker state (mirrors ProviderPanel's Models block) -----
	let fetching = $state(false);
	let fetchError = $state('');
	let fetchSource = $state<'preset' | 'upstream' | ''>('');
	let fetchResults = $state<
		(ProviderModelEntry & { checked: boolean })[]
	>([]);
	let saving = $state(false);
	let filter = $state('');

	$effect(() => { onBusyChange?.(saving || fetching); });
	onMount(() => { void loadModels(); });

	async function loadModels() {
		if (!providerId) return;
		const selectedIds = new Set(fetchResults.filter((r) => r.checked).map((r) => r.id));
		fetching = true;
		fetchError = '';
		fetchSource = '';
		try {
			const res = await providers.fetchProviderModels(providerId);
			fetchSource = res.source;
			fetchResults = (res.models ?? []).map((m) => ({
				id: m.id,
				label: m.label,
				checked: selectedIds.has(m.id),
				context_window: m.context_window,
				...copyReasoningMetadata(m)
			}));
		} catch (e) {
			fetchError = getErrorMessage(e, 'Could not fetch models');
		} finally {
			fetching = false;
		}
	}

	function toggleResult(id: string) {
		fetchResults = fetchResults.map((r) => (r.id === id ? { ...r, checked: !r.checked } : r));
	}

	let filteredResults = $derived(
		filter.trim()
			? fetchResults.filter((r) => {
					const q = filter.trim().toLowerCase();
					return r.id.toLowerCase().includes(q) || r.label.toLowerCase().includes(q);
				})
			: fetchResults
	);
	let selectedCount = $derived(fetchResults.filter((r) => r.checked).length);

	async function useSelected() {
		if (!providerId || saving || fetching || selectedCount === 0) return;
		// Preserve the upstream per-model window (OpenRouter publishes
		// `context_length`); rebuilding as a bare {id,label} would drop it and
		// flatten every model onto the row's single shared window.
		const selected = fetchResults
			.filter((r) => r.checked)
			.map((r) => ({
				id: r.id.trim(),
				label: (r.label || r.id).trim(),
				...(r.context_window != null ? { context_window: r.context_window } : {}),
				...copyReasoningMetadata(r)
			}))
			.filter((m) => m.id && m.label);
		saving = true;
		fetchError = '';
		try {
			await updateModelProvider(providerId, { models: selected });
			await providers.refreshProviders();
			onDone();
		} catch (e) {
			fetchError = getErrorMessage(e, 'Could not save models');
		} finally {
			saving = false;
		}
	}

</script>

<div class="space-y-4">
	<!-- 'pick' step: choose the models to offer from the fetched catalog. -->
	<div class="flex items-center justify-between gap-3">
		<p class="text-sm text-muted-foreground">
			{#if fetching}
				Fetching models…
			{:else if fetchSource === 'preset'}
				Preset catalog — check the models to offer.
			{:else}
				Fetched {fetchResults.length} {fetchResults.length === 1 ? 'model' : 'models'}. Check the ones to offer.
			{/if}
		</p>
		<button
			type="button"
			onclick={loadModels}
			disabled={fetching || saving}
			class="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
		>
			{#if fetching}<Loader2 class="w-3.5 h-3.5 animate-spin" />{:else}<RefreshCw class="w-3.5 h-3.5" />{/if}
			Refresh
		</button>
	</div>

	{#if fetchError}
		<div class="p-3 bg-destructive/10 border border-destructive/20 rounded-lg text-sm text-destructive" role="alert">
			{fetchError}
		</div>
	{/if}

	{#if !fetching && fetchResults.length > 0}
		<input
			type="text"
			bind:value={filter}
			placeholder="Filter models…"
			aria-label="Filter models"
			class="w-full px-3 py-2 bg-background border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:ring-2 focus:ring-ring"
		/>
		<div class="flex flex-col gap-0.5 max-h-72 overflow-y-auto rounded-lg border border-border p-1">
			{#each filteredResults as r (r.id)}
				<label class="flex items-center gap-2 px-2 py-1.5 rounded-md text-sm cursor-pointer hover:bg-muted">
					<input
						type="checkbox"
						checked={r.checked}
						disabled={saving}
						onchange={() => toggleResult(r.id)}
						class="shrink-0 cursor-pointer"
					/>
					<span class="font-mono text-xs truncate">{r.id}</span>
					{#if r.label && r.label !== r.id}
						<span class="text-xs text-muted-foreground truncate">{r.label}</span>
					{/if}
				</label>
			{/each}
			{#if filteredResults.length === 0}
				<p class="px-2 py-1.5 text-sm text-muted-foreground">No models match “{filter}”.</p>
			{/if}
		</div>
	{:else if !fetching && !fetchError}
		<p class="text-sm text-muted-foreground">No models returned. You can add them later by editing the provider.</p>
	{/if}

	<div class="flex items-center justify-end gap-3 pt-1">
		<button
			type="button"
			onclick={onDone}
			disabled={saving || fetching}
			class="px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
		>
			Choose later
		</button>
		<button
			type="button"
			onclick={useSelected}
			disabled={saving || fetching || selectedCount === 0}
			class="px-4 py-2 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 flex items-center gap-2"
		>
			{#if saving}<Loader2 class="w-4 h-4 animate-spin" />Saving…{:else}Use selected{selectedCount ? ` (${selectedCount})` : ''}{/if}
		</button>
	</div>
</div>
