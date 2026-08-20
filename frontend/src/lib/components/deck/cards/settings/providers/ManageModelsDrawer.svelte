<script lang="ts">
	/**
	 * ManageModelsDrawer — the "Manage models" drawer for a gateway provider row
	 * (plan §1.4).
	 *
	 * Opened from a gateway row's "Manage models" action. Two stacked panels in one
	 * surface:
	 *   1. A fetch-models check-select picker (filter + checkbox list) that curates
	 *      which models the provider offers. Mechanics mirror the 'pick' step of
	 *      `flows/OpenRouterKeyFlow.svelte` (copied, NOT imported).
	 *   2. The four model-params fields (default / small-fast / context-window /
	 *      max-output), seeded from the provider row exactly as `ProviderEditForm`
	 *      seeds them.
	 *
	 * Save persists the curated model subset AND the four params in one
	 * `updateModelProvider` call, then re-hydrates via `providers.refreshProviders()`
	 * (landmine L4) and closes.
	 *
	 * Rendered OUTSIDE `.ed-content` (fixed-position drawer) → self-contained
	 * Tailwind theme-token utility classes ONLY, immune to shell globals
	 * (landmine L3). Right-side drawer above 1080px; fullscreen at ≤1080px via
	 * `isPanelFullscreenWidth` (mirrors `AddProviderModal`).
	 */
	import { onMount, untrack } from 'svelte';
	import { X, Loader2, RefreshCw } from 'lucide-svelte';
	import { isPanelFullscreenWidth } from '$lib/utils/viewport';
	import { getErrorMessage } from '$lib/api/client';
	import { updateModelProvider, type ModelProvider } from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';

	interface Props {
		provider: ModelProvider;
		onClose: () => void;
	}
	let { provider, onClose }: Props = $props();

	// A fresh instance mounts per open, so seed ONCE from the provider snapshot and
	// never react to later prop changes (mirrors ProviderEditForm). `untrack`
	// captures the initial value without a reactive dependency (and silences the
	// state_referenced_locally warning a bare prop read would raise).
	const seed = untrack(() => provider);

	// ----- Fullscreen at handheld widths (mirrors AddProviderModal) -----
	let viewportWidth = $state(typeof window !== 'undefined' ? window.innerWidth : 1280);
	let fullscreen = $derived(isPanelFullscreenWidth(viewportWidth));

	// Esc is handled via keydown on the dialog container, which only fires while
	// focus is inside it — so focus it on mount.
	let dialogEl: HTMLDivElement;

	// ----- Model params, seeded from the row (mirrors ProviderEditForm ~60-63) ---
	let defaultModel = $state(seed.default_model ?? '');
	let smallFastModel = $state(seed.small_fast_model ?? '');
	let contextWindow = $state(seed.context_window != null ? String(seed.context_window) : '');
	let maxOutputTokens = $state(seed.max_output_tokens != null ? String(seed.max_output_tokens) : '');

	// ----- Fetch-models picker state (mirrors OpenRouterKeyFlow 'pick') -----
	let fetching = $state(false);
	let fetchError = $state('');
	let fetchSource = $state<'preset' | 'upstream' | ''>('');
	let results = $state<
		{
			id: string;
			label: string;
			checked: boolean;
			context_window?: number | null;
			pricing?: import('$lib/api/model-providers').ProviderModelEntry['pricing'];
		}[]
	>([]);
	let filter = $state('');

	let saving = $state(false);
	let saveError = $state('');

	onMount(() => {
		dialogEl?.focus();
		const onResize = () => (viewportWidth = window.innerWidth);
		window.addEventListener('resize', onResize);
		void loadModels();
		return () => window.removeEventListener('resize', onResize);
	});

	async function loadModels() {
		fetching = true;
		fetchError = '';
		fetchSource = '';
		try {
			const res = await providers.fetchProviderModels(seed.id);
			fetchSource = res.source;

			const stored = seed.models ?? [];
			const hasCurated = stored.length > 0;
			const catalog = res.models ?? [];
			const catalogIds = new Set(catalog.map((m) => m.id));
			const curatedIds = new Set(stored.map((m) => m.id));

			// Stored models absent from the fetched catalog must still appear (checked)
			// so a save doesn't silently drop them — prepend with their stored label.
			const extras = stored
				.filter((m) => !catalogIds.has(m.id))
				.map((m) => ({
					id: m.id,
					label: m.label,
					checked: true,
					context_window: m.context_window,
					pricing: m.pricing
				}));

			// Pre-check: if the row already has curated models, check exactly those ids.
			// Otherwise pre-check the ENTIRE preset roster (source='preset') and NOTHING
			// for an upstream fetch (plan §1.4).
			//
			// A stored per-model window wins over the freshly-fetched one (an admin
			// curation is deliberate); otherwise take whatever the catalog advertises.
			const storedWindows = new Map(stored.map((m) => [m.id, m.context_window]));
			const storedPricing = new Map(stored.map((m) => [m.id, m.pricing]));
			const catalogRows = catalog.map((m) => ({
				id: m.id,
				label: m.label,
				checked: hasCurated ? curatedIds.has(m.id) : fetchSource === 'preset',
				context_window: storedWindows.get(m.id) ?? m.context_window,
				pricing: storedPricing.get(m.id) ?? m.pricing
			}));

			results = [...extras, ...catalogRows];
		} catch (e) {
			fetchError = getErrorMessage(e, 'Could not fetch models');
			// Keep the stored curation visible and checked so a Save after a
			// failed fetch can't silently wipe the provider's models.
			results = (seed.models ?? []).map((m) => ({
				id: m.id,
				label: m.label,
				checked: true,
				context_window: m.context_window,
				pricing: m.pricing
			}));
		} finally {
			fetching = false;
		}
	}

	function toggleResult(id: string) {
		results = results.map((r) => (r.id === id ? { ...r, checked: !r.checked } : r));
	}

	let filteredResults = $derived(
		filter.trim()
			? results.filter((r) => {
					const q = filter.trim().toLowerCase();
					return r.id.toLowerCase().includes(q) || r.label.toLowerCase().includes(q);
				})
			: results
	);
	let selectedCount = $derived(results.filter((r) => r.checked).length);

	// Parse an optional positive integer field; '' or non-finite → null (omit).
	// bind:value on a type="number" input coerces to number|null in Svelte 5,
	// so this receives the seeded string OR a coerced number/null after edits.
	function parseOptionalInt(value: string | number | null): number | null {
		if (value == null) return null;
		const trimmed = String(value).trim();
		if (!trimmed) return null;
		const n = Number(trimmed);
		return Number.isFinite(n) && n > 0 ? Math.floor(n) : null;
	}

	async function save() {
		if (saving) return;
		// Per-model `context_window` (from the upstream catalog or a previous
		// curation) is PRESERVED — rebuilding entries as a bare {id,label} used to
		// silently drop it, which forced every model back onto the row's single
		// shared window. There is no editor for it here; we just don't lose it.
		const models = results
			.filter((r) => r.checked)
			.map((r) => ({
				id: r.id.trim(),
				label: (r.label || r.id).trim(),
				...(r.context_window != null ? { context_window: r.context_window } : {}),
				...(r.pricing ? { pricing: r.pricing } : {})
			}))
			.filter((m) => m.id && m.label);
		saving = true;
		saveError = '';
		try {
			await updateModelProvider(seed.id, {
				models,
				default_model: defaultModel || null,
				small_fast_model: smallFastModel || null,
				context_window: parseOptionalInt(contextWindow),
				max_output_tokens: parseOptionalInt(maxOutputTokens)
			});
			await providers.refreshProviders();
			onClose();
		} catch (e) {
			saveError = getErrorMessage(e, 'Could not save models');
		} finally {
			saving = false;
		}
	}

	function requestClose() {
		if (!saving) onClose();
	}
	function handleKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') requestClose();
	}
</script>

<div
	bind:this={dialogEl}
	class="fixed inset-0 z-50 flex justify-end"
	role="dialog"
	aria-modal="true"
	aria-label="Manage models — {seed.name}"
	onkeydown={handleKeydown}
	tabindex="-1"
>
	<!-- Backdrop -->
	<button
		class="absolute inset-0 bg-background/80 backdrop-blur-sm"
		onclick={requestClose}
		aria-label="Close"
	></button>

	<!-- Drawer panel -->
	<div
		class="relative flex h-full flex-col bg-card shadow-xl {fullscreen
			? 'w-full max-w-none'
			: 'w-full max-w-lg border-l border-border'}"
	>
		<!-- Header -->
		<div class="flex items-start gap-2 px-6 py-4 border-b border-border">
			<div class="min-w-0 flex-1">
				<h2 class="truncate text-lg font-semibold text-foreground">{seed.name}</h2>
				<p class="text-xs text-muted-foreground mt-0.5">Manage models</p>
			</div>
			<button
				type="button"
				onclick={requestClose}
				disabled={saving}
				class="p-2 -mr-2 hover:bg-muted rounded-lg transition-colors disabled:opacity-50"
				aria-label="Close"
			>
				<X class="w-5 h-5 text-muted-foreground" />
			</button>
		</div>

		<!-- Body -->
		<div class="flex-1 overflow-y-auto p-6 space-y-6">
			<!-- Picker -->
			<div class="space-y-3">
				<div class="flex items-center justify-between gap-3">
					<p class="text-sm text-muted-foreground">
						{#if fetching}
							Fetching models…
						{:else if fetchSource === 'preset'}
							Preset catalog — edit as needed.
						{:else}
							Fetched {results.length} {results.length === 1 ? 'model' : 'models'}. Check the ones
							to offer.
						{/if}
					</p>
					<button
						type="button"
						onclick={loadModels}
						disabled={fetching || saving}
						class="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
					>
						{#if fetching}<Loader2 class="w-3.5 h-3.5 animate-spin" />{:else}<RefreshCw
								class="w-3.5 h-3.5"
							/>{/if}
						Refresh
					</button>
				</div>

				{#if fetchError}
					<div
						class="p-3 bg-destructive/10 border border-destructive/20 rounded-lg text-sm text-destructive"
						role="alert"
					>
						{fetchError}
					</div>
				{/if}

				{#if !fetching && results.length > 0}
					<input
						type="text"
						bind:value={filter}
						placeholder="Filter models…"
						class="w-full px-3 py-2 bg-background border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
					/>
					<div
						class="flex flex-col gap-0.5 max-h-72 overflow-y-auto rounded-lg border border-border p-1"
					>
						{#each filteredResults as r (r.id)}
							<label
								class="flex items-center gap-2 px-2 py-1.5 rounded-md text-sm cursor-pointer hover:bg-muted"
							>
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
					<p class="text-sm text-muted-foreground">
						No models returned. Try Refresh, or set them when the catalog is available.
					</p>
				{/if}
			</div>

			<!-- Model params -->
			<div class="space-y-4 border-t border-border pt-5">
				<div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
					<div>
						<label
							for="mm-default"
							class="block text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-1.5"
							>Default model</label
						>
						<input
							id="mm-default"
							type="text"
							bind:value={defaultModel}
							placeholder="e.g. openai/gpt-5.6"
							spellcheck="false"
							autocapitalize="off"
							autocomplete="off"
							class="w-full px-3 py-2 bg-background border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
						/>
					</div>
					<div>
						<label
							for="mm-smallfast"
							class="block text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-1.5"
							>Small / fast model</label
						>
						<input
							id="mm-smallfast"
							type="text"
							bind:value={smallFastModel}
							placeholder="e.g. openai/gpt-5.6-mini"
							spellcheck="false"
							autocapitalize="off"
							autocomplete="off"
							class="w-full px-3 py-2 bg-background border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
						/>
					</div>
					<div>
						<label
							for="mm-ctx"
							class="block text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-1.5"
							>Context window <span class="normal-case font-normal opacity-75">(optional)</span></label
						>
						<input
							id="mm-ctx"
							type="number"
							min="1"
							bind:value={contextWindow}
							placeholder="e.g. 200000"
							class="w-full px-3 py-2 bg-background border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
						/>
					</div>
					<div>
						<label
							for="mm-maxout"
							class="block text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-1.5"
							>Max output tokens <span class="normal-case font-normal opacity-75">(optional)</span
							></label
						>
						<input
							id="mm-maxout"
							type="number"
							min="1"
							bind:value={maxOutputTokens}
							placeholder="e.g. 8192"
							class="w-full px-3 py-2 bg-background border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
						/>
					</div>
				</div>
			</div>
		</div>

		<!-- Footer -->
		<div class="border-t border-border px-6 py-4">
			{#if saveError}
				<div
					class="mb-3 p-3 bg-destructive/10 border border-destructive/20 rounded-lg text-sm text-destructive"
					role="alert"
				>
					{saveError}
				</div>
			{/if}
			<div class="flex items-center justify-end gap-3">
				<button
					type="button"
					onclick={requestClose}
					disabled={saving}
					class="px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
				>
					Cancel
				</button>
				<button
					type="button"
					onclick={save}
					disabled={saving || fetching}
					class="px-4 py-2 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 flex items-center gap-2"
				>
					{#if saving}<Loader2 class="w-4 h-4 animate-spin" />Saving…{:else}Save{selectedCount
							? ` (${selectedCount})`
							: ''}{/if}
				</button>
			</div>
		</div>
	</div>
</div>
