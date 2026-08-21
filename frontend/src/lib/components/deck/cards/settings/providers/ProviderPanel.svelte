<script lang="ts">
	/**
	 * ProviderPanel — the body of an expanded provider row.
	 *
	 * Every row in the catalog renders THIS component, so the panel structure can
	 * never drift between providers. Top to bottom:
	 *
	 *   1. Action row  — exactly two buttons: the primary (`Sign in` on a
	 *                    subscription row, `Add key` on a key/gateway row) and
	 *                    `Remove` with an inline confirm. `Remove` is dropped when
	 *                    the row has nothing configured.
	 *   2. Settings    — Name / Endpoint / Auth token (+ API dialect on custom
	 *                    rows, + optional context window and max output tokens),
	 *                    with one `Save` that is live only while the form is dirty.
	 *                    API-key / gateway rows ONLY: a subscription row's endpoint
	 *                    is pinned by its OAuth flow, and editing it breaks the row.
	 *   3. Models      — a filterable checkbox list of the provider's catalog, each
	 *                    toggle persisted immediately. The two native rows (Claude,
	 *                    Anthropic API key) render the same block read-only from the
	 *                    model registry, since native models are not curatable.
	 *   4. Banked resets — the ChatGPT / Grok reset inventory, inline.
	 *
	 * The panel owns only its own form/list state. Modals, delete confirms and the
	 * provider store calls that change what the catalog contains stay in
	 * `ProviderList`; this component reports back through its callbacks.
	 */
	import { onMount, untrack, type Snippet } from 'svelte';
	import { Loader2, RefreshCw } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		updateModelProvider,
		type ModelProvider,
		type ProviderModelEntry
	} from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';
	import { toast } from '$lib/stores/toast';
	import { getModels } from '$lib/constants/models.svelte';
	import BankedResetsPanel from './BankedResetsPanel.svelte';

	interface Props {
		/** Drives the primary button's wording: `Sign in` vs `Add key`. */
		kind: 'subscription' | 'key';
		/** The `model_providers` row behind this slot, or null when there is none. */
		provider?: ModelProvider | null;
		/** Claude / Anthropic-key rows: models come from the registry, read-only. */
		nativeModels?: boolean;
		/** Whether anything is configured — `Remove` renders only when it is. */
		configured?: boolean;
		/** Custom gateway rows expose the wire-format select. */
		showDialect?: boolean;
		/** ChatGPT / Grok rows with a live credential get the reset inventory. */
		bankedResets?: boolean;

		/**
		 * Primary action. Omitted on a key row that already has a stored row —
		 * there the button focuses the token field instead of opening anything.
		 */
		onPrimary?: () => void;
		/** Remove confirm — armed and resolved by the parent. */
		confirming?: boolean;
		onArmRemove?: () => void;
		onConfirmRemove?: () => void;
		onCancelRemove?: () => void;

		/** Row-specific extras (status notices, the Anthropic replace-key form). */
		afterActions?: Snippet;
	}

	let {
		kind,
		provider = null,
		nativeModels = false,
		configured = false,
		showDialect = false,
		bankedResets = false,
		onPrimary,
		confirming = false,
		onArmRemove,
		onConfirmRemove,
		onCancelRemove,
		afterActions
	}: Props = $props();

	const uid = $props.id();

	// Settings are for API-key / gateway rows only. A subscription row's endpoint
	// is fixed by its OAuth flow, so exposing Name / Endpoint / Auth token there
	// would only let someone break the row. The rule is by ROW KIND, not by
	// whether a stored row happens to exist.
	let showSettings = $derived(kind === 'key' && provider !== null);

	// The panel mounts fresh whenever its row expands, so the form seeds ONCE from
	// the row snapshot and never reacts to later prop changes (which would discard
	// half-typed edits every time the store refreshes). `untrack` takes that first
	// value without creating a dependency.
	const seed = untrack(() => provider);

	// ----- Settings form ------------------------------------------------------
	let name = $state(seed?.name ?? '');
	let baseUrl = $state(seed?.base_url ?? '');
	let apiDialect = $state<'anthropic' | 'openai'>(seed?.api_dialect === 'openai' ? 'openai' : 'anthropic');
	let token = $state('');
	let contextWindow = $state<string | number | null>(
		seed?.context_window != null ? String(seed.context_window) : ''
	);
	let maxOutputTokens = $state<string | number | null>(
		seed?.max_output_tokens != null ? String(seed.max_output_tokens) : ''
	);
	let savingSettings = $state(false);
	let settingsError = $state('');
	let tokenEl = $state<HTMLInputElement | null>(null);

	// What "unchanged" means right now. Reset after each successful save so the
	// Save button goes back to inert instead of staying lit against a stale seed.
	let baseline = $state({
		name: seed?.name ?? '',
		baseUrl: seed?.base_url ?? '',
		dialect: seed?.api_dialect === 'openai' ? 'openai' : 'anthropic',
		contextWindow: seed?.context_window ?? null,
		maxOutputTokens: seed?.max_output_tokens ?? null
	});

	/** Parse an optional positive integer field; '' or non-finite → null (omit). */
	function parseOptionalInt(value: string | number | null): number | null {
		if (value == null) return null;
		const trimmed = String(value).trim();
		if (!trimmed) return null;
		const n = Number(trimmed);
		return Number.isFinite(n) && n > 0 ? Math.floor(n) : null;
	}

	let dirty = $derived(
		name.trim() !== baseline.name ||
			baseUrl.trim() !== baseline.baseUrl ||
			apiDialect !== baseline.dialect ||
			parseOptionalInt(contextWindow) !== baseline.contextWindow ||
			parseOptionalInt(maxOutputTokens) !== baseline.maxOutputTokens ||
			token.trim().length > 0
	);

	let tokenPlaceholder = $derived(
		seed?.auth_token_set
			? `${seed.auth_token_masked || '••••••••'} — leave blank to keep`
			: 'Provider API key'
	);

	async function saveSettings() {
		if (!provider || savingSettings || !dirty) return;
		const nextName = name.trim();
		const nextUrl = baseUrl.trim();
		if (!nextName || !nextUrl) {
			settingsError = 'Name and endpoint are both required.';
			return;
		}
		savingSettings = true;
		settingsError = '';
		try {
			// Everything this form does not author is passed straight back through:
			// the row's type, its curated models (with each entry's own context
			// window and pricing), its small/fast pick and its default model.
			const providerType = provider.provider_type;
			const models = (provider.models ?? []).map((m) => ({
				id: m.id,
				label: m.label,
				...(m.context_window != null ? { context_window: m.context_window } : {}),
				...(m.pricing ? { pricing: m.pricing } : {})
			}));
			const nextToken = token.trim();
			await providers.saveProvider(
				{
					name: nextName,
					provider_type: providerType,
					base_url: nextUrl,
					// Only a real custom gateway authors its dialect. The Codex row is
					// also provider_type 'custom' but its wire format is fixed, so its
					// stored value is left alone rather than rewritten on every save.
					...(showDialect ? { api_dialect: apiDialect } : {}),
					models,
					default_model: provider.default_model,
					small_fast_model: provider.small_fast_model,
					context_window: parseOptionalInt(contextWindow),
					max_output_tokens: parseOptionalInt(maxOutputTokens),
					// Write-only: send a token only when one was typed.
					...(nextToken ? { auth_token: nextToken } : {})
				},
				provider.id
			);
			token = '';
			baseline = {
				name: nextName,
				baseUrl: nextUrl,
				dialect: apiDialect,
				contextWindow: parseOptionalInt(contextWindow),
				maxOutputTokens: parseOptionalInt(maxOutputTokens)
			};
		} catch (e) {
			settingsError = getErrorMessage(e, 'Save failed');
		} finally {
			savingSettings = false;
		}
	}

	// `Add key` on a row that already has one replaces it in place: there is no
	// second form, so the button just puts the cursor in the token field.
	function handlePrimary() {
		if (showSettings) {
			tokenEl?.focus();
			return;
		}
		onPrimary?.();
	}

	let primaryLabel = $derived(kind === 'subscription' ? 'Sign in' : 'Add key');

	// ----- Models -------------------------------------------------------------
	interface ModelRow {
		id: string;
		label: string;
		checked: boolean;
		context_window?: number | null;
		pricing?: ProviderModelEntry['pricing'];
	}

	let results = $state<ModelRow[]>([]);
	let modelsLoading = $state(false);
	let modelsError = $state('');
	let modelsSaving = $state(false);
	let filter = $state('');

	let checkedCount = $derived(results.filter((r) => r.checked).length);
	let filteredResults = $derived.by(() => {
		const q = filter.trim().toLowerCase();
		if (!q) return results;
		return results.filter((r) => r.id.toLowerCase().includes(q) || r.label.toLowerCase().includes(q));
	});

	const nativeModelList = $derived(getModels());

	onMount(() => {
		if (provider) void loadModels();
	});

	async function loadModels() {
		if (!provider || modelsLoading) return;
		const id = provider.id;
		const stored = provider.models ?? [];
		modelsLoading = true;
		modelsError = '';
		try {
			const res = await providers.fetchProviderModels(id);
			const catalog = res.models ?? [];
			const catalogIds = new Set(catalog.map((m) => m.id));
			const curatedIds = new Set(stored.map((m) => m.id));
			const hasCurated = stored.length > 0;

			// Stored models absent from the fetched catalog must still appear
			// (checked) so a save doesn't silently drop them.
			const extras = stored
				.filter((m) => !catalogIds.has(m.id))
				.map((m) => ({
					id: m.id,
					label: m.label,
					checked: true,
					context_window: m.context_window,
					pricing: m.pricing
				}));

			// Pre-check: an already-curated row checks exactly its own ids;
			// otherwise the whole preset roster, and nothing for an upstream fetch.
			// A stored per-model window wins over the freshly-fetched one.
			const storedWindows = new Map(stored.map((m) => [m.id, m.context_window]));
			const storedPricing = new Map(stored.map((m) => [m.id, m.pricing]));
			const catalogRows = catalog.map((m) => ({
				id: m.id,
				label: m.label,
				checked: hasCurated ? curatedIds.has(m.id) : res.source === 'preset',
				context_window: storedWindows.get(m.id) ?? m.context_window,
				pricing: storedPricing.get(m.id) ?? m.pricing
			}));

			results = [...extras, ...catalogRows];
		} catch (e) {
			modelsError = getErrorMessage(e, 'Could not fetch models');
			// Keep the stored curation visible and checked so a failed fetch can't
			// lead to silently wiping the provider's models.
			results = stored.map((m) => ({
				id: m.id,
				label: m.label,
				checked: true,
				context_window: m.context_window,
				pricing: m.pricing
			}));
		} finally {
			modelsLoading = false;
		}
	}

	function curatedEntries(rows: ModelRow[]): ProviderModelEntry[] {
		return rows
			.filter((r) => r.checked)
			.map((r) => ({
				id: r.id.trim(),
				label: (r.label || r.id).trim(),
				...(r.context_window != null ? { context_window: r.context_window } : {}),
				...(r.pricing ? { pricing: r.pricing } : {})
			}))
			.filter((m) => m.id && m.label);
	}

	async function toggleModel(id: string) {
		if (!provider || modelsSaving) return;
		const previous = results;
		const next = results.map((r) => (r.id === id ? { ...r, checked: !r.checked } : r));
		results = next;
		modelsSaving = true;
		try {
			await updateModelProvider(provider.id, { models: curatedEntries(next) });
			await providers.refreshProviders();
		} catch (e) {
			results = previous;
			toast.push(getErrorMessage(e, 'Could not save models'), { tone: 'error' });
		} finally {
			modelsSaving = false;
		}
	}
</script>

<!-- 1. Action row — same two buttons on every row. -->
<div class="prov-actrow">
	{#if confirming}
		<span class="acct-confirm">Remove?</span>
		<button type="button" class="ed-btn danger acct-sm" onclick={() => onConfirmRemove?.()}>
			Remove
		</button>
		<button type="button" class="ed-btn acct-sm" onclick={() => onCancelRemove?.()}>Cancel</button>
	{:else}
		<button type="button" class="ed-btn primary acct-sm" onclick={handlePrimary}>
			{primaryLabel}
		</button>
		{#if configured}
			<button type="button" class="ed-btn danger acct-sm" onclick={() => onArmRemove?.()}>
				Remove
			</button>
		{/if}
	{/if}
</div>

{@render afterActions?.()}

{#if showSettings}
	<!-- 2. Settings — compact inline fields for the stored row. -->
	<div class="pp-block">
		<div class="pp-blockhead"><span class="pp-blocktitle">Settings</span></div>
		<div class="pp-grid">
			<label class="pp-lab" for="pp-name-{uid}">Name</label>
			<input id="pp-name-{uid}" type="text" class="inp pp-inp" bind:value={name} />

			<label class="pp-lab" for="pp-url-{uid}">Endpoint</label>
			<input
				id="pp-url-{uid}"
				type="text"
				class="inp pp-inp pp-mono"
				bind:value={baseUrl}
				spellcheck="false"
				autocapitalize="off"
				autocomplete="off"
			/>

			<label class="pp-lab" for="pp-token-{uid}">Auth token</label>
			<input
				bind:this={tokenEl}
				id="pp-token-{uid}"
				type="password"
				class="inp pp-inp"
				bind:value={token}
				placeholder={tokenPlaceholder}
				autocomplete="off"
				spellcheck="false"
			/>

			{#if showDialect}
				<label class="pp-lab" for="pp-dialect-{uid}">API dialect</label>
				<select id="pp-dialect-{uid}" class="sel pp-inp" bind:value={apiDialect}>
					<option value="anthropic">Anthropic Messages</option>
					<option value="openai">OpenAI chat/completions</option>
				</select>
			{/if}

			<label class="pp-lab" for="pp-ctx-{uid}">Context window</label>
			<input
				id="pp-ctx-{uid}"
				type="number"
				min="1"
				class="inp pp-inp"
				bind:value={contextWindow}
				placeholder="Optional"
			/>

			<label class="pp-lab" for="pp-maxout-{uid}">Max output tokens</label>
			<input
				id="pp-maxout-{uid}"
				type="number"
				min="1"
				class="inp pp-inp"
				bind:value={maxOutputTokens}
				placeholder="Optional"
			/>
		</div>

		<div class="prov-actrow pp-saverow">
			<button
				type="button"
				class="ed-btn acct-sm"
				onclick={saveSettings}
				disabled={!dirty || savingSettings}
			>
				{#if savingSettings}<Loader2 class="pp-spin" />Saving…{:else}Save{/if}
			</button>
		</div>
		{#if settingsError}
			<p class="pp-err" role="alert">{settingsError}</p>
		{/if}
	</div>
{/if}

{#if provider}
	<!-- 3. Models — curated inline, each toggle saved on the spot. -->
	<div class="pp-block">
		<div class="pp-blockhead">
			<span class="pp-blocktitle">
				Models · {checkedCount} of {results.length}
			</span>
			<button
				type="button"
				class="pp-iconbtn"
				onclick={loadModels}
				disabled={modelsLoading || modelsSaving}
				aria-label="Refresh models"
				title="Refresh models"
			>
				{#if modelsLoading}<Loader2 class="pp-spin" />{:else}<RefreshCw />{/if}
			</button>
		</div>

		{#if modelsError}
			<p class="pp-err" role="alert">{modelsError}</p>
		{/if}

		{#if modelsLoading && results.length === 0}
			<p class="pp-muted">Loading…</p>
		{:else if results.length === 0}
			<p class="pp-muted">No models returned.</p>
		{:else}
			<input
				type="text"
				class="inp pp-inp pp-filter"
				bind:value={filter}
				placeholder="Filter models…"
				aria-label="Filter models"
			/>
			<div class="pp-mlist">
				{#each filteredResults as r (r.id)}
					<label class="pp-mrow">
						<input
							type="checkbox"
							checked={r.checked}
							disabled={modelsSaving}
							onchange={() => toggleModel(r.id)}
						/>
						<span class="pp-mid">{r.id}</span>
						{#if r.label && r.label !== r.id}<span class="pp-mlabel">{r.label}</span>{/if}
					</label>
				{/each}
				{#if filteredResults.length === 0}
					<p class="pp-muted pp-mempty">No models match “{filter}”.</p>
				{/if}
			</div>
		{/if}
	</div>
{:else if nativeModels}
	<!-- 3. Models — the native roster. There is no curation API for it, so the
	     block keeps the same shape and reads as a list. -->
	<div class="pp-block">
		<div class="pp-blockhead"><span class="pp-blocktitle">Models · built in</span></div>
		<div class="pp-mlist">
			{#each nativeModelList as m (m.value)}
				<span class="pp-mrow pp-mrow-static">
					<span class="pp-mid">{m.value}</span>
					<span class="pp-mlabel">{m.label}</span>
				</span>
			{/each}
		</div>
	</div>
{/if}

{#if bankedResets && provider}
	<!-- 4. Banked resets — ChatGPT / Grok only. -->
	<div class="pp-block">
		<BankedResetsPanel {provider} />
	</div>
{/if}

<style>
	.prov-actrow {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}
	.acct-sm {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 5px 10px;
		font-size: 12px;
	}
	.acct-confirm {
		font-size: 12px;
		color: var(--warning, #d97706);
		font-weight: 600;
	}

	/* Each numbered block gets the same container so every row reads alike. */
	.pp-block {
		margin-top: 12px;
		padding-top: 10px;
		border-top: 1px solid var(--border);
	}
	.pp-blockhead {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin-bottom: 8px;
	}
	.pp-blocktitle {
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		color: var(--muted-foreground);
	}

	/* Label column left, control right. */
	.pp-grid {
		display: grid;
		grid-template-columns: 110px minmax(0, 1fr);
		align-items: center;
		gap: 6px 10px;
	}
	.pp-lab {
		font-size: 12px;
		color: var(--muted-foreground);
	}
	.pp-inp.inp,
	.pp-inp.sel {
		width: 100%;
		min-width: 0;
		height: 30px;
		padding: 0 9px;
		font-size: 12px;
		border-radius: 7px;
	}
	.pp-inp.sel {
		padding-right: 28px;
	}
	.pp-mono.inp {
		font-family: var(--font-mono, monospace);
		font-size: 11.5px;
	}
	.pp-saverow {
		margin-top: 10px;
	}
	.pp-err {
		margin: 6px 0 0;
		font-size: 11.5px;
		line-height: 1.4;
		color: var(--destructive);
	}
	.pp-muted {
		margin: 0;
		font-size: 12px;
		color: var(--muted-foreground);
	}

	.pp-iconbtn {
		display: grid;
		place-items: center;
		width: 26px;
		height: 26px;
		flex-shrink: 0;
		border-radius: 7px;
		border: 1px solid var(--border);
		background: var(--background);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 0.15s, border-color 0.15s;
	}
	.pp-iconbtn:hover:not(:disabled) {
		color: var(--foreground);
		border-color: var(--foreground);
	}
	.pp-iconbtn:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.pp-iconbtn :global(svg) {
		width: 13px;
		height: 13px;
	}

	.pp-filter {
		margin-bottom: 6px;
	}
	.pp-mlist {
		display: flex;
		flex-direction: column;
		gap: 1px;
		max-height: 220px;
		overflow-y: auto;
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 4px;
	}
	.pp-mrow {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
		padding: 4px 6px;
		border-radius: 6px;
		font-size: 12px;
		cursor: pointer;
	}
	.pp-mrow:hover {
		background: var(--muted);
	}
	.pp-mrow input {
		flex-shrink: 0;
		cursor: pointer;
	}
	.pp-mrow-static {
		cursor: default;
	}
	.pp-mrow-static:hover {
		background: none;
	}
	.pp-mid {
		font-family: var(--font-mono, monospace);
		font-size: 11.5px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pp-mlabel {
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pp-mempty {
		padding: 4px 6px;
	}

	:global(.pp-spin) {
		width: 13px;
		height: 13px;
		animation: pp-spin 0.8s linear infinite;
	}
	@keyframes pp-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		:global(.pp-spin) {
			animation: none;
		}
	}

	@media (max-width: 560px) {
		.pp-grid {
			grid-template-columns: minmax(0, 1fr);
			gap: 3px;
		}
		.pp-lab {
			margin-top: 6px;
		}
	}
</style>
