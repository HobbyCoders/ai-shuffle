<script lang="ts">
	/**
	 * ProviderEditForm — the add / edit form for a gateway `model_providers` row.
	 *
	 * A clean lift of the "mp-form" block (add/edit + the Chunk-D fetch-models
	 * catalog picker) out of `ConnectionsSection`. Mechanics are UNCHANGED — the
	 * only rewire is that the two persist paths now go through the providers store
	 * instead of the component's own `loadProviders`/`refreshProviders`:
	 *   - main save  → `providers.saveProvider(body, editingProviderId)`
	 *   - fetch-picker models-only persist → `updateModelProvider(id, { models })`
	 *     followed by `providers.refreshProviders()` (the store has no models-only
	 *     helper; this reproduces today's exact two-step).
	 *
	 * Form state stays component-local (plan §2.4). The parent (`ProviderList`)
	 * mounts one instance per open form and closes it via `onClose`.
	 *
	 * provider_type: for OpenRouter/Custom presets the preset drives the type, as
	 * before. For an already-saved provider whose type is neither (e.g. `openai`,
	 * or the Codex `custom`+chatgpt row should it ever reach here) the ORIGINAL
	 * type is preserved on save so editing never silently rewrites it to `custom`.
	 */
	import { RefreshCw, Loader2, Plus, X } from 'lucide-svelte';
	import { untrack } from 'svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		updateModelProvider,
		type ModelProvider,
		type ProviderModelEntry
	} from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';

	type ProviderPreset = 'openrouter' | 'custom';
	const OPENROUTER_BASE_URL = 'https://openrouter.ai/api';

	interface Props {
		/** Row being edited, or `null`/omitted to add a new provider. */
		provider?: ModelProvider | null;
		/** Close the form (after a successful save, or on cancel). */
		onClose: () => void;
	}
	let { provider = null, onClose }: Props = $props();

	// A fresh instance mounts per edit, so the form seeds ONCE from the provider
	// snapshot and never reacts to later prop changes. `untrack` captures that
	// initial value without creating a reactive dependency (and silences the
	// state_referenced_locally warning that a bare prop read would raise).
	const seed = untrack(() => provider);

	const editingProviderId = seed?.id ?? null;
	// Preserved so a non-openrouter/custom provider keeps its type on save.
	const originalProviderType = seed?.provider_type ?? null;

	let formPreset: ProviderPreset = $state(seed?.provider_type === 'openrouter' ? 'openrouter' : 'custom');
	let formName = $state(seed?.name ?? '');
	let formBaseUrl = $state(seed?.base_url ?? OPENROUTER_BASE_URL);
	let formToken = $state('');
	// On edit, the masked existing token shown as the placeholder (blank = keep it).
	let formTokenPlaceholder = $state(seed?.auth_token_set ? seed.auth_token_masked || '••••••••' : '');
	let formModels: ProviderModelEntry[] = $state((seed?.models ?? []).map((m) => ({ id: m.id, label: m.label })));
	let formDefaultModel = $state(seed?.default_model ?? '');
	let formSmallFastModel = $state(seed?.small_fast_model ?? '');
	let formContextWindow = $state(seed?.context_window != null ? String(seed.context_window) : '');
	let formMaxOutputTokens = $state(seed?.max_output_tokens != null ? String(seed.max_output_tokens) : '');
	let formSaving = $state(false);
	let formError = $state('');

	// ----- Fetch-models catalog picker (Chunk D) -----
	let showFetchPicker = $state(false);
	let fetchingModels = $state(false);
	let fetchError = $state('');
	let fetchSource = $state<'preset' | 'upstream' | ''>('');
	let fetchResults = $state<{ id: string; label: string; checked: boolean }[]>([]);
	let fetchSaving = $state(false);

	async function openFetchModels() {
		if (!editingProviderId) return;
		fetchingModels = true;
		fetchError = '';
		fetchSource = '';
		try {
			const res = await providers.fetchProviderModels(editingProviderId);
			const curated = new Set(formModels.map((m) => m.id.trim()).filter(Boolean));
			fetchSource = res.source;
			fetchResults = (res.models ?? []).map((m) => ({
				id: m.id,
				label: m.label,
				// Preselect ids already in the curated set.
				checked: curated.has(m.id)
			}));
			showFetchPicker = true;
		} catch (e) {
			fetchError = getErrorMessage(e, 'Could not fetch models');
			showFetchPicker = true;
		} finally {
			fetchingModels = false;
		}
	}

	function toggleFetchResult(index: number) {
		fetchResults = fetchResults.map((r, i) => (i === index ? { ...r, checked: !r.checked } : r));
	}

	function cancelFetchPicker() {
		showFetchPicker = false;
		fetchResults = [];
		fetchError = '';
		fetchSource = '';
	}

	// Confirm: the checked entries become the curated set, persisted through the
	// existing update call (no second write path). The in-form list is kept in
	// sync so a subsequent Save doesn't clobber it.
	async function confirmFetchSelection() {
		if (!editingProviderId) return;
		const selected = fetchResults
			.filter((r) => r.checked)
			.map((r) => ({ id: r.id.trim(), label: (r.label || r.id).trim() }))
			.filter((m) => m.id && m.label);
		fetchSaving = true;
		fetchError = '';
		try {
			await updateModelProvider(editingProviderId, { models: selected });
			formModels = selected.map((m) => ({ id: m.id, label: m.label }));
			showFetchPicker = false;
			fetchResults = [];
			fetchSource = '';
			await providers.refreshProviders();
		} catch (e) {
			fetchError = getErrorMessage(e, 'Could not save models');
		} finally {
			fetchSaving = false;
		}
	}

	// Curated ids offered to the default / small-fast selects (blanks excluded).
	let selectableModelIds = $derived(formModels.map((m) => m.id.trim()).filter((id) => id.length > 0));

	// Preset choice drives the base_url prefill: OpenRouter fills the canonical
	// gateway URL, Custom clears it for a hand-entered endpoint.
	function selectPreset(preset: ProviderPreset) {
		formPreset = preset;
		formBaseUrl = preset === 'openrouter' ? OPENROUTER_BASE_URL : '';
	}

	function addModelRow() {
		formModels = [...formModels, { id: '', label: '' }];
	}
	function removeModelRow(index: number) {
		formModels = formModels.filter((_, i) => i !== index);
	}

	// Parse an optional positive integer field; '' or non-finite → null (omit).
	function parseOptionalInt(value: string): number | null {
		const trimmed = value.trim();
		if (!trimmed) return null;
		const n = Number(trimmed);
		return Number.isFinite(n) && n > 0 ? Math.floor(n) : null;
	}

	// Keep a saved provider's original type unless it was an OpenRouter/Custom row
	// (where the preset is the source of truth). Prevents editing an `openai` row
	// from silently rewriting it to `custom`.
	function resolvedProviderType(): string {
		if (
			editingProviderId &&
			originalProviderType &&
			originalProviderType !== 'openrouter' &&
			originalProviderType !== 'custom'
		) {
			return originalProviderType;
		}
		return formPreset;
	}

	async function saveProvider() {
		const name = formName.trim();
		const baseUrl = formBaseUrl.trim();
		if (!name || !baseUrl) return;
		// Keep only fully-specified model rows so the backend never rejects a
		// half-typed entry (both id and label are required).
		const models = formModels
			.map((m) => ({ id: m.id.trim(), label: m.label.trim() }))
			.filter((m) => m.id && m.label);
		const token = formToken.trim();

		formSaving = true;
		formError = '';
		try {
			const body = {
				name,
				provider_type: resolvedProviderType(),
				base_url: baseUrl,
				models,
				default_model: formDefaultModel || null,
				small_fast_model: formSmallFastModel || null,
				context_window: parseOptionalInt(formContextWindow),
				max_output_tokens: parseOptionalInt(formMaxOutputTokens),
				// Write-only: only send a non-blank token (blank = leave stored value).
				...(token ? { auth_token: token } : {})
			};
			await providers.saveProvider(body, editingProviderId);
			onClose();
		} catch (e) {
			formError = getErrorMessage(e, 'Save failed');
		} finally {
			formSaving = false;
		}
	}
</script>

<div class="acct-addpanel mp-form">
	<!-- Preset selector: OpenRouter prefills the gateway URL; Custom is blank. -->
	<div class="mp-field">
		<span class="mp-label">Preset</span>
		<div class="mp-presets">
			<button type="button" class="ed-btn mp-preset" class:primary={formPreset === 'openrouter'} onclick={() => selectPreset('openrouter')}>OpenRouter</button>
			<button type="button" class="ed-btn mp-preset" class:primary={formPreset === 'custom'} onclick={() => selectPreset('custom')}>Custom</button>
		</div>
	</div>

	<div class="mp-field">
		<label class="mp-label" for="mp-name">Name</label>
		<input id="mp-name" type="text" class="inp" bind:value={formName} placeholder="e.g. OpenRouter" />
	</div>

	<div class="mp-field">
		<label class="mp-label" for="mp-baseurl">Base URL</label>
		<input id="mp-baseurl" type="text" class="inp" bind:value={formBaseUrl} placeholder="https://openrouter.ai/api" spellcheck="false" autocapitalize="off" autocomplete="off" />
	</div>

	<div class="mp-field">
		<label class="mp-label" for="mp-token">Auth token</label>
		<input
			id="mp-token"
			type="password"
			class="inp"
			autocomplete="off"
			bind:value={formToken}
			placeholder={editingProviderId && formTokenPlaceholder ? `${formTokenPlaceholder} — leave blank to keep` : 'Provider API key'}
		/>
	</div>

	<div class="mp-field">
		<div class="mp-models-head">
			<span class="mp-label">Models</span>
			{#if editingProviderId}
				<button type="button" class="ed-btn mp-fetch-btn" onclick={openFetchModels} disabled={fetchingModels || fetchSaving}>
					{#if fetchingModels}<Loader2 class="spin" />Fetching…{:else}<RefreshCw />Fetch models{/if}
				</button>
			{/if}
		</div>
		<div class="mp-models">
			{#if editingProviderId == null}
				<p class="mp-hint">Save the provider first to fetch its model catalog.</p>
			{/if}

			{#if showFetchPicker}
				<div class="mp-fetch-panel">
					{#if fetchError}
						<div class="kc-foot err mp-formerr" role="alert">{fetchError}</div>
					{:else}
						<p class="mp-hint">
							{#if fetchSource === 'preset'}Preset catalog — edit as needed, then confirm.{:else}Fetched {fetchResults.length} {fetchResults.length === 1 ? 'model' : 'models'}. Check the ones to offer.{/if}
						</p>
						<div class="mp-fetch-list">
							{#each fetchResults as r, i (r.id)}
								<label class="mp-fetch-row">
									<input type="checkbox" checked={r.checked} onchange={() => toggleFetchResult(i)} />
									<span class="mp-fetch-id">{r.id}</span>
									{#if r.label && r.label !== r.id}<span class="mp-fetch-label">{r.label}</span>{/if}
								</label>
							{/each}
							{#if fetchResults.length === 0}<p class="mp-hint">No models returned.</p>{/if}
						</div>
					{/if}
					<div class="acct-addrow">
						{#if !fetchError}
							<button type="button" class="ed-btn primary" onclick={confirmFetchSelection} disabled={fetchSaving}>
								{#if fetchSaving}<Loader2 class="spin" />Saving…{:else}Use selected{/if}
							</button>
						{/if}
						<button type="button" class="ed-btn" onclick={cancelFetchPicker} disabled={fetchSaving}>Cancel</button>
					</div>
				</div>
			{/if}

			{#if formModels.length === 0}
				<p class="mp-hint">No models yet. Add the gateway model ids you want to offer (e.g. <code>openai/gpt-5.6</code>).</p>
			{/if}
			{#each formModels as model, i (i)}
				<div class="mp-modelrow">
					<input type="text" class="inp mp-modelid" bind:value={model.id} placeholder="openai/gpt-5.6" spellcheck="false" autocapitalize="off" autocomplete="off" aria-label="Model id" />
					<input type="text" class="inp mp-modellabel" bind:value={model.label} placeholder="GPT-5.6" aria-label="Model label" />
					<button type="button" class="acct-iconbtn danger" title="Remove model" aria-label="Remove model" onclick={() => removeModelRow(i)}><X /></button>
				</div>
			{/each}
			<button type="button" class="ed-btn mp-addmodel" onclick={addModelRow}><Plus />Add model</button>
		</div>
	</div>

	<div class="mp-field-row">
		<div class="mp-field">
			<label class="mp-label" for="mp-default">Default model</label>
			<select id="mp-default" class="inp" bind:value={formDefaultModel}>
				<option value="">— none —</option>
				{#each selectableModelIds as id}
					<option value={id}>{id}</option>
				{/each}
			</select>
		</div>
		<div class="mp-field">
			<label class="mp-label" for="mp-smallfast">Small / fast model</label>
			<select id="mp-smallfast" class="inp" bind:value={formSmallFastModel}>
				<option value="">— none —</option>
				{#each selectableModelIds as id}
					<option value={id}>{id}</option>
				{/each}
			</select>
		</div>
	</div>

	<div class="mp-field-row">
		<div class="mp-field">
			<label class="mp-label" for="mp-ctx">Context window <span class="mp-optional">(optional)</span></label>
			<input id="mp-ctx" type="number" min="1" class="inp" bind:value={formContextWindow} placeholder="e.g. 200000" />
		</div>
		<div class="mp-field">
			<label class="mp-label" for="mp-maxout">Max output tokens <span class="mp-optional">(optional)</span></label>
			<input id="mp-maxout" type="number" min="1" class="inp" bind:value={formMaxOutputTokens} placeholder="e.g. 8192" />
		</div>
	</div>

	{#if formError}<div class="kc-foot err mp-formerr" role="alert">{formError}</div>{/if}

	<div class="acct-addrow">
		<button type="button" class="ed-btn primary" onclick={saveProvider} disabled={formSaving || !formName.trim() || !formBaseUrl.trim()}>
			{#if formSaving}<Loader2 class="spin" />Saving…{:else}{editingProviderId ? 'Save changes' : 'Add provider'}{/if}
		</button>
		<button type="button" class="ed-btn" onclick={onClose} disabled={formSaving}>Cancel</button>
	</div>
</div>

<style>
	/* Add/edit form styles relocated from ConnectionsSection (unchanged). Rendered
	   inside `.ed-content`, so shell globals (.ed-btn/.inp) still theme controls. */
	.acct-addpanel {
		margin-top: 10px;
		padding: 12px;
		border: 1px dashed var(--border);
		border-radius: 10px;
		background: var(--muted);
	}
	.acct-addrow {
		display: flex;
		gap: 8px;
	}
	.mp-form {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	.mp-field {
		display: flex;
		flex-direction: column;
		gap: 5px;
		min-width: 0;
		flex: 1;
	}
	.mp-field-row {
		display: flex;
		gap: 12px;
	}
	.mp-label {
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		color: var(--muted-foreground);
	}
	.mp-optional {
		font-weight: 500;
		text-transform: none;
		letter-spacing: 0;
		opacity: 0.75;
	}
	.mp-field .inp {
		width: 100%;
	}
	.mp-presets {
		display: flex;
		gap: 8px;
	}
	.mp-preset {
		flex: 1;
	}
	.mp-models {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.mp-hint {
		margin: 0;
		font-size: 12px;
		line-height: 1.5;
		color: var(--muted-foreground);
	}
	.mp-hint code {
		font-family: var(--font-mono, monospace);
		font-size: 11px;
	}
	.mp-modelrow {
		display: flex;
		gap: 8px;
		align-items: center;
	}
	.mp-modelid {
		flex: 2;
		min-width: 0;
		font-family: var(--font-mono, monospace);
		font-size: 12px;
	}
	.mp-modellabel {
		flex: 1;
		min-width: 0;
	}
	.mp-addmodel {
		align-self: flex-start;
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.mp-addmodel :global(svg) {
		width: 14px;
		height: 14px;
	}
	.mp-models-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
	}
	.mp-fetch-btn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.mp-fetch-btn :global(svg) {
		width: 14px;
		height: 14px;
	}
	.mp-fetch-panel {
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 10px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--muted);
	}
	.mp-fetch-list {
		display: flex;
		flex-direction: column;
		gap: 2px;
		max-height: 240px;
		overflow-y: auto;
	}
	.mp-fetch-row {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 5px 6px;
		border-radius: 6px;
		font-size: 13px;
		cursor: pointer;
	}
	.mp-fetch-row:hover {
		background: var(--background);
	}
	.mp-fetch-row input {
		flex-shrink: 0;
		cursor: pointer;
	}
	.mp-fetch-id {
		font-family: var(--font-mono, monospace);
		font-size: 12px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.mp-fetch-label {
		color: var(--muted-foreground);
		font-size: 12px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.kc-foot {
		padding: 8px 18px 12px;
		font-size: 12.5px;
	}
	.kc-foot.err {
		color: var(--destructive);
	}
	.mp-formerr {
		padding: 0;
	}
	.acct-iconbtn {
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: var(--background);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 0.15s, border-color 0.15s, background 0.15s;
	}
	.acct-iconbtn :global(svg) {
		width: 15px;
		height: 15px;
	}
	.acct-iconbtn:hover {
		color: var(--foreground);
		border-color: var(--foreground);
	}
	.acct-iconbtn.danger:hover {
		color: var(--destructive);
		border-color: var(--destructive);
	}
	.acct-iconbtn:focus-visible {
		outline: 2px solid var(--ring, var(--foreground));
		outline-offset: 1px;
	}
	/* Saving spinner inside buttons. */
	:global(.mp-form .ed-btn .spin) {
		width: 14px;
		height: 14px;
		animation: mp-spin 0.8s linear infinite;
	}
	@keyframes mp-spin {
		to { transform: rotate(360deg); }
	}
	@media (prefers-reduced-motion: reduce) {
		:global(.mp-form .ed-btn .spin) {
			animation: none;
		}
	}

	@media (max-width: 560px) {
		.mp-field-row {
			flex-direction: column;
		}
		.mp-modelrow {
			flex-wrap: wrap;
		}
		.mp-modelid,
		.mp-modellabel {
			flex: 1 1 100%;
		}
	}
</style>
