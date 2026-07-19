<script lang="ts">
	/**
	 * OpenRouterKeyFlow — the "OpenRouter API key" Add-provider flow
	 * (plan §1.2 `openrouter-key`, create-then-fetch §1.4).
	 *
	 * Two internal steps:
	 *   'key'  — key field + "Continue". Creates the provider
	 *            (`createModelProvider`, so we get the `id` for the fetch step),
	 *            re-hydrates the registry, then advances to 'pick'.
	 *   'pick' — the fetch-models check-select picker. Reuses the exact mechanics
	 *            `ProviderEditForm.svelte` uses (fetch → check-select → persist the
	 *            curated subset via `updateModelProvider(id, { models })` →
	 *            `refreshProviders()`), copied here (NOT imported) with Tailwind
	 *            classes for L3 immunity. "Use selected" saves + closes; "Cancel"
	 *            KEEPS the created row (valid with 0 models — user can edit later)
	 *            and closes.
	 *
	 * Rendered inside `AddProviderModal` (outside `.ed-content`) → self-contained
	 * Tailwind utility classes only (landmine L3).
	 */
	import { Loader2, RefreshCw } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { createModelProvider, updateModelProvider } from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';

	interface Props {
		onDone: () => void;
		onCancel: () => void;
		onBusyChange?: (busy: boolean) => void;
	}
	let { onDone, onCancel, onBusyChange }: Props = $props();

	type Step = 'key' | 'pick';
	let step = $state<Step>('key');

	// The id of the row created in the 'key' step, needed for fetch + persist.
	let createdId = $state<string | null>(null);

	let key = $state('');
	let creating = $state(false);
	let error = $state('');

	// ----- Fetch-models picker state (mirrors ProviderEditForm) -----
	let fetching = $state(false);
	let fetchError = $state('');
	let fetchSource = $state<'preset' | 'upstream' | ''>('');
	let fetchResults = $state<{ id: string; label: string; checked: boolean }[]>([]);
	let saving = $state(false);
	let filter = $state('');

	// Busy = anything that must guard Esc / backdrop. Fetching mid-picker is NOT
	// busy in the create sense, but a save or create is.
	$effect(() => {
		onBusyChange?.(creating || saving || fetching);
	});

	async function createAndContinue() {
		const token = key.trim();
		if (!token || creating) return;
		creating = true;
		error = '';
		try {
			const created = await createModelProvider({
				name: 'OpenRouter',
				provider_type: 'openrouter',
				base_url: 'https://openrouter.ai/api/v1',
				auth_token: token
			});
			createdId = created.id;
			// The row exists now — surface it in the list immediately.
			await providers.refreshProviders();
			step = 'pick';
			await loadModels();
		} catch (e) {
			error = getErrorMessage(e, 'Could not create OpenRouter provider');
		} finally {
			creating = false;
		}
	}

	async function loadModels() {
		if (!createdId) return;
		fetching = true;
		fetchError = '';
		fetchSource = '';
		try {
			const res = await providers.fetchProviderModels(createdId);
			fetchSource = res.source;
			fetchResults = (res.models ?? []).map((m) => ({ id: m.id, label: m.label, checked: false }));
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
		if (!createdId || saving) return;
		const selected = fetchResults
			.filter((r) => r.checked)
			.map((r) => ({ id: r.id.trim(), label: (r.label || r.id).trim() }))
			.filter((m) => m.id && m.label);
		saving = true;
		fetchError = '';
		try {
			await updateModelProvider(createdId, { models: selected });
			await providers.refreshProviders();
			onDone();
		} catch (e) {
			fetchError = getErrorMessage(e, 'Could not save models');
		} finally {
			saving = false;
		}
	}

	// 'key' step Cancel → back to the choose screen (nothing was created).
	// 'pick' step Cancel → the row is already created and valid with 0 models;
	// keep it (do NOT delete) and just close the modal.
	function cancel() {
		if (step === 'pick') {
			onDone();
		} else {
			onCancel();
		}
	}
</script>

<div class="space-y-4">
	{#if step === 'key'}
		<p class="text-sm text-muted-foreground leading-relaxed">
			Paste your OpenRouter API key. Next you'll pick which of its models to offer.
		</p>

		{#if error}
			<div class="p-3 bg-destructive/10 border border-destructive/20 rounded-lg text-sm text-destructive" role="alert">
				{error}
			</div>
		{/if}

		<div>
			<label for="openrouter-key" class="block text-sm font-medium text-foreground mb-1.5">API key</label>
			<input
				id="openrouter-key"
				type="password"
				autocomplete="off"
				spellcheck="false"
				bind:value={key}
				placeholder="sk-or-…"
				disabled={creating}
				class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
			/>
		</div>

		<div class="flex items-center justify-end gap-3 pt-1">
			<button
				type="button"
				onclick={cancel}
				disabled={creating}
				class="px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
			>
				Back
			</button>
			<button
				type="button"
				onclick={createAndContinue}
				disabled={creating || !key.trim()}
				class="px-4 py-2 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 flex items-center gap-2"
			>
				{#if creating}<Loader2 class="w-4 h-4 animate-spin" />Creating…{:else}Continue{/if}
			</button>
		</div>
	{:else}
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
				class="w-full px-3 py-2 bg-background border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
			/>
			<div class="flex flex-col gap-0.5 max-h-72 overflow-y-auto rounded-lg border border-border p-1">
				{#each filteredResults as r (r.id)}
					<label class="flex items-center gap-2 px-2 py-1.5 rounded-md text-sm cursor-pointer hover:bg-muted">
						<input
							type="checkbox"
							checked={r.checked}
							onchange={() => toggleResult(r.id)}
							class="flex-shrink-0 cursor-pointer"
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
				onclick={cancel}
				disabled={saving}
				class="px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
			>
				Cancel
			</button>
			<button
				type="button"
				onclick={useSelected}
				disabled={saving || fetching}
				class="px-4 py-2 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 flex items-center gap-2"
			>
				{#if saving}<Loader2 class="w-4 h-4 animate-spin" />Saving…{:else}Use selected{selectedCount ? ` (${selectedCount})` : ''}{/if}
			</button>
		</div>
	{/if}
</div>
