<script lang="ts">
	import { onMount } from 'svelte';
	import {
		AlertTriangle,
		ArrowLeft,
		ArrowRight,
		Check,
		ChevronRight,
		FileInput,
		FolderSearch,
		Info,
		LoaderCircle,
		RefreshCw,
		ShieldCheck,
		X
	} from 'lucide-svelte';
	import { getErrorMessage, isApiError } from '$lib/api/client';
	import {
		applySettingsImport,
		previewSettingsImport,
		type SettingsImportApplyResponse,
		type SettingsImportCategory,
		type SettingsImportItem,
		type SettingsImportOutcome,
		type SettingsImportPreview,
		type SettingsImportProvider
	} from '$lib/api/settings-import';
	import { appData } from '$lib/stores/app-data.svelte';
	import { platform } from '$lib/stores/platform.svelte';

	interface Props {
		onClose: () => void;
	}

	let { onClose }: Props = $props();

	type Step = 'source' | 'preview' | 'confirm' | 'result';
	type ProviderChoice = 'both' | SettingsImportProvider;

	const providerOptions: Array<{ value: ProviderChoice; label: string; detail: string }> = [
		{
			value: 'both',
			label: 'Claude + Codex',
			detail: 'Scan both; exact duplicates collapse and differing instructions become review sidecars.'
		},
		{ value: 'claude', label: 'Claude', detail: 'Scan ~/.claude and the Claude project registry.' },
		{ value: 'codex', label: 'Codex', detail: 'Scan ~/.codex and Codex-configured projects.' }
	];

	let step = $state<Step>('source');
	let providerChoice = $state<ProviderChoice>('both');
	let sourceHome = $state('');
	let preview = $state<SettingsImportPreview | null>(null);
	let result = $state<SettingsImportApplyResponse | null>(null);
	let selectedIds = $state(new Set<string>());
	let previewing = $state(false);
	let applying = $state(false);
	let confirmCopy = $state(false);
	let error = $state<string | null>(null);
	let stalePlan = $state(false);
	let liveMessage = $state('Choose which provider settings to preview.');

	let selectedProviders = $derived<SettingsImportProvider[]>(
		providerChoice === 'both' ? ['codex', 'claude'] : [providerChoice]
	);

	let dialogEl = $state<HTMLDivElement | null>(null);
	let cardEl = $state<HTMLDivElement | null>(null);
	/* Fullscreen on the mobile shell; a centred card, clamped to the viewport,
	   on the desktop shell at every width. */
	const fullscreen = $derived(platform.isMobile);
	let busy = $derived(previewing || applying);
	let items = $derived(preview?.categories.flatMap((category) => category.items) ?? []);
	let selectedItems = $derived(items.filter((item) => selectedIds.has(item.id)));
	let conflictCount = $derived.by(() => {
		const conflicts = new Set(preview?.conflicts ?? []);
		for (const item of items) {
			if (item.conflict || ['conflict', 'duplicate'].includes(item.status)) {
				conflicts.add(item.conflict || item.id);
			}
		}
		return conflicts.size;
	});

	onMount(() => {
		queueMicrotask(() => dialogEl?.focus());
	});

	function requestClose(): void {
		if (!busy) onClose();
	}

	function focusableElements(): HTMLElement[] {
		if (!cardEl) return [];
		return Array.from(
			cardEl.querySelectorAll<HTMLElement>(
				'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'
			)
		).filter((element) => !element.hasAttribute('hidden'));
	}

	function handleDialogKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') {
			event.preventDefault();
			requestClose();
			return;
		}
		if (event.key !== 'Tab') return;

		const focusable = focusableElements();
		if (focusable.length === 0) {
			event.preventDefault();
			dialogEl?.focus();
			return;
		}
		const first = focusable[0];
		const last = focusable[focusable.length - 1];
		const active = document.activeElement;
		if (event.shiftKey && (active === first || active === dialogEl)) {
			event.preventDefault();
			last.focus();
		} else if (!event.shiftKey && (active === last || active === dialogEl)) {
			event.preventDefault();
			first.focus();
		}
	}

	function providerSummary(providers: SettingsImportProvider[] = selectedProviders): string {
		if (providers.length === 2) return 'Claude + Codex';
		return providers[0] === 'codex' ? 'Codex' : 'Claude';
	}

	function isUnavailable(item: SettingsImportItem): boolean {
		return !item.selectable || !!item.conflict || ['conflict', 'duplicate'].includes(item.status);
	}

	async function loadPreview(): Promise<void> {
		previewing = true;
		error = null;
		stalePlan = false;
		result = null;
		liveMessage = `Scanning ${providerSummary()} settings. Nothing is being changed.`;
		try {
			const nextPreview = await previewSettingsImport({
				providers: selectedProviders,
				...(sourceHome.trim() ? { source_home: sourceHome.trim() } : {})
			});
			preview = nextPreview;
			selectedIds = new Set(
				nextPreview.categories
					.flatMap((category) => category.items)
					.filter((item) => item.selectable && item.selected_by_default && !item.conflict)
					.map((item) => item.id)
			);
			confirmCopy = false;
			step = 'preview';
			liveMessage = `Preview ready. ${selectedIds.size} ${selectedIds.size === 1 ? 'item' : 'items'} selected.`;
		} catch (cause) {
			error = getErrorMessage(cause, 'Provider settings could not be scanned.');
			liveMessage = error;
		} finally {
			previewing = false;
		}
	}

	function toggleItem(item: SettingsImportItem): void {
		if (isUnavailable(item)) return;
		const next = new Set(selectedIds);
		if (next.has(item.id)) next.delete(item.id);
		else next.add(item.id);
		selectedIds = next;
		confirmCopy = false;
	}

	function selectableItems(category: SettingsImportCategory): SettingsImportItem[] {
		return category.items.filter((item) => !isUnavailable(item));
	}

	function categorySelected(category: SettingsImportCategory): boolean {
		const categoryItems = selectableItems(category);
		return categoryItems.length > 0 && categoryItems.every((item) => selectedIds.has(item.id));
	}

	function toggleCategory(category: SettingsImportCategory): void {
		const categoryItems = selectableItems(category);
		const shouldSelect = !categorySelected(category);
		const next = new Set(selectedIds);
		for (const item of categoryItems) {
			if (shouldSelect) next.add(item.id);
			else next.delete(item.id);
		}
		selectedIds = next;
		confirmCopy = false;
	}

	function openConfirmation(): void {
		if (selectedItems.length === 0) return;
		confirmCopy = false;
		error = null;
		step = 'confirm';
		liveMessage = `Confirm import of ${selectedItems.length} ${selectedItems.length === 1 ? 'item' : 'items'}.`;
	}

	function backToPreview(): void {
		confirmCopy = false;
		error = null;
		step = 'preview';
	}

	async function applyImport(): Promise<void> {
		if (!preview || !confirmCopy || selectedItems.length === 0) return;
		applying = true;
		error = null;
		liveMessage = 'Importing selected settings. Existing destination files will not be overwritten.';
		try {
			result = await applySettingsImport({
				token: preview.token,
				selected_item_ids: selectedItems.map((item) => item.id),
				providers: preview.providers,
				...(sourceHome.trim() ? { source_home: sourceHome.trim() } : {})
			});
			if (result.imported_projects.length > 0) await appData.loadProjects();
			step = 'result';
			liveMessage = `Import finished. ${result.imported.length} imported, ${result.skipped.length} skipped, ${result.conflicts.length} conflicts, ${result.errors.length} errors.`;
		} catch (cause) {
			if (isApiError(cause) && cause.status === 409) {
				stalePlan = true;
				preview = null;
				selectedIds = new Set();
				confirmCopy = false;
				step = 'source';
				error = 'This preview is out of date. Preview the source again before importing.';
			} else {
				error = getErrorMessage(cause, 'Selected settings could not be imported.');
			}
			liveMessage = error;
		} finally {
			applying = false;
		}
	}

	function statusLabel(item: SettingsImportItem): string {
		if (item.status === 'duplicate') return 'Duplicate';
		if (item.conflict || item.status === 'conflict') return 'Conflict';
		if (item.status === 'diagnostic') return 'Diagnostic';
		if (item.status === 'blocked') return 'Blocked';
		if (item.status === 'unsupported') return 'Not supported';
		if (!item.selectable) return 'Inventory only';
		if (item.legacy_detected || item.status === 'legacy') return 'Legacy';
		return item.status === 'available' || item.status === 'ready' ? 'Ready' : item.status;
	}

	function statusKind(item: SettingsImportItem): 'ready' | 'warning' | 'muted' {
		if (item.conflict || ['conflict', 'duplicate', 'blocked', 'error'].includes(item.status)) {
			return 'warning';
		}
		if (!item.selectable || item.status === 'diagnostic') return 'muted';
		return 'ready';
	}

	function outcomeTitle(group: 'imported' | 'skipped' | 'conflicts' | 'errors'): string {
		return {
			imported: 'Imported',
			skipped: 'Skipped',
			conflicts: 'Conflicts',
			errors: 'Errors'
		}[group];
	}

	function outcomeHint(group: 'imported' | 'skipped' | 'conflicts' | 'errors'): string {
		return {
			imported: 'Created in AI Shuffle',
			skipped: 'No destination change',
			conflicts: 'Existing destination kept',
			errors: 'Not imported'
		}[group];
	}

	function outcomeItems(group: 'imported' | 'skipped' | 'conflicts' | 'errors'): SettingsImportOutcome[] {
		return result?.[group] ?? [];
	}
</script>

<div
	bind:this={dialogEl}
	class="fixed inset-0 z-50 flex items-center justify-center {fullscreen ? 'p-0' : 'p-4'}"
	role="dialog"
	aria-modal="true"
	aria-labelledby="settings-import-title"
	tabindex="-1"
	onkeydown={handleDialogKeydown}
>
	<button
		type="button"
		class="absolute inset-0 bg-background/80 backdrop-blur-xs"
		aria-label="Close provider settings import"
		tabindex="-1"
		disabled={busy}
		onclick={requestClose}
	></button>

	<div
		bind:this={cardEl}
		class="relative flex flex-col overflow-hidden border border-border bg-card shadow-xl {fullscreen
			? 'h-full w-full max-w-none rounded-none'
			: 'max-h-[92vh] w-full max-w-3xl rounded-xl'}"
	>
		<header class="flex items-center gap-3 border-b border-border px-5 py-4 sm:px-6">
			<div class="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-border bg-muted text-foreground" aria-hidden="true">
				<FileInput class="h-4 w-4" />
			</div>
			<div class="min-w-0 flex-1">
				<h2 id="settings-import-title" class="text-base font-semibold text-foreground sm:text-lg">Import provider settings</h2>
				<p class="mt-0.5 text-xs text-muted-foreground">Preview Claude and Codex sources before copying anything</p>
			</div>
			<button
				type="button"
				onclick={requestClose}
				disabled={busy}
				class="-mr-3 grid min-h-11 min-w-11 place-items-center rounded-lg text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
				aria-label="Close"
			>
				<X class="h-5 w-5" />
			</button>
		</header>

		<div class="progress" aria-label="Import progress">
			<span class:active={step === 'source'} class:done={step !== 'source'}>Source</span>
			<ChevronRight aria-hidden="true" />
			<span class:active={step === 'preview'} class:done={step === 'confirm' || step === 'result'}>Preview &amp; select</span>
			<ChevronRight aria-hidden="true" />
			<span class:active={step === 'confirm'} class:done={step === 'result'}>Apply</span>
			<ChevronRight aria-hidden="true" />
			<span class:active={step === 'result'}>Result</span>
		</div>

		<p class="sr-only" aria-live="polite">{liveMessage}</p>

		<div class="flex-1 overflow-y-auto p-5 sm:p-6">
			{#if step === 'source'}
				<div class="mx-auto max-w-xl space-y-5">
					<div class="safety-panel">
						<ShieldCheck aria-hidden="true" />
						<div>
							<h3>Fluid stays authoritative.</h3>
							<p>Preview is read-only. Existing Fluid-native files always win, sources stay untouched, and the import never overwrites a destination.</p>
						</div>
					</div>

					<fieldset class="provider-fieldset" disabled={previewing}>
						<legend>Providers to scan</legend>
						<div class="provider-grid">
							{#each providerOptions as option}
								<label class="provider-card">
									<input type="radio" name="settings-import-provider" value={option.value} bind:group={providerChoice} />
									<span class="provider-mark" aria-hidden="true">{option.value === 'both' ? '2' : option.value === 'codex' ? 'CX' : 'CL'}</span>
									<span><strong>{option.label}</strong><small>{option.detail}</small></span>
								</label>
							{/each}
						</div>
					</fieldset>

					<div>
						<label for="settings-source-home" class="mb-2 block text-sm font-medium text-foreground">Settings home <span class="font-normal text-muted-foreground">(optional)</span></label>
						<div class="relative">
							<FolderSearch class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
							<input
								id="settings-source-home"
								type="text"
								bind:value={sourceHome}
								disabled={previewing}
								placeholder="Use the detected server home"
								autocomplete="off"
								spellcheck="false"
								class="source-input"
							/>
						</div>
						<p class="mt-2 text-xs leading-relaxed text-muted-foreground">Leave blank to use the detected home containing <code>~/.claude</code> and/or <code>~/.codex</code>. For Docker, use the configured read-only import mount path.</p>
					</div>

					<div class="info-panel">
						<Info aria-hidden="true" />
						<p><strong>MCP details are redacted.</strong> Server names may appear in settings and plugins inventory, but credentials and environment values are never shown, copied, or activated.</p>
					</div>

					{#if error}
						<div class="error-panel" role="alert">
							{#if stalePlan}<RefreshCw aria-hidden="true" />{:else}<AlertTriangle aria-hidden="true" />{/if}
							<div>
								<p>{error}</p>
								{#if stalePlan}<span>The previous selection was not applied.</span>{/if}
							</div>
						</div>
					{/if}

					<button type="button" class="primary-button w-full sm:w-auto" onclick={loadPreview} disabled={previewing}>
						{#if previewing}<LoaderCircle class="animate-spin" />Scanning…{:else if stalePlan}<RefreshCw />Preview again{:else}<FolderSearch />Preview settings{/if}
					</button>
				</div>
			{:else if step === 'preview' && preview}
				<div class="space-y-5">
					<div class="route-panel">
						<div>
							<span>{providerSummary(preview.providers)} source home</span>
							<code>{preview.source_display_path || 'Detected source'}</code>
						</div>
						<ArrowRight aria-hidden="true" />
						<div>
							<span>Fluid-native destination</span>
							<code>{preview.destination_display_path || 'Managed Fluid settings'}</code>
						</div>
					</div>

					<div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
						<div>
							<h3 class="text-sm font-semibold text-foreground">Choose what to copy</h3>
							<p class="mt-1 text-xs leading-relaxed text-muted-foreground">{selectedItems.length} of {items.length} items selected. Unselected and conflicting items will not change the destination.</p>
						</div>
						<button type="button" class="secondary-button shrink-0" onclick={() => { step = 'source'; error = null; }}>
							<RefreshCw />New preview
						</button>
					</div>

					{#if preview.legacy_detected}
						<div class="info-panel">
							<Info aria-hidden="true" />
							<p><strong>Provider settings detected.</strong> They remain inert in AI Shuffle unless you select and apply them.</p>
						</div>
					{/if}

					{#if conflictCount > 0}
						<div class="warning-panel" role="status">
							<AlertTriangle aria-hidden="true" />
							<div>
								<p><strong>{conflictCount} protected or duplicate {conflictCount === 1 ? 'item' : 'items'}.</strong> Fluid-native destinations win; duplicate provider sources and existing files cannot be selected.</p>
								{#if preview.conflicts.length > 0}
									<ul>{#each preview.conflicts as conflict}<li>{conflict}</li>{/each}</ul>
								{/if}
							</div>
						</div>
					{/if}

					{#if preview.categories.length === 0 || items.length === 0}
						<div class="empty-panel">
							<FolderSearch aria-hidden="true" />
							<h3>No importable settings found</h3>
							<p>Check the source path, then run a new preview.</p>
						</div>
					{:else}
						<div class="space-y-3">
							{#each preview.categories as category (category.id)}
								<section class="category-card" aria-labelledby={`category-${category.id}`}>
									<div class="category-head">
										<label class="category-toggle">
											<input
												type="checkbox"
												checked={categorySelected(category)}
												disabled={selectableItems(category).length === 0}
												onchange={() => toggleCategory(category)}
												aria-label={`Select all ${category.label}`}
											/>
											<span class="checkmark"><Check /></span>
										</label>
										<div class="min-w-0 flex-1">
											<h4 id={`category-${category.id}`}>{category.label}</h4>
											{#if category.description}<p>{category.description}</p>{/if}
											{#if category.id === 'settings_plugins_inventory'}<p class="inventory-note">Inventory only—settings, plugins, and redacted MCP metadata are not activated.</p>{/if}
										</div>
										<span class="count-chip">{category.items.length}</span>
									</div>
									<ul class="item-list">
										{#each category.items as item (item.id)}
											<li class:unavailable={isUnavailable(item)}>
												<label class="item-toggle">
													<input
														type="checkbox"
														checked={selectedIds.has(item.id)}
														disabled={isUnavailable(item)}
														onchange={() => toggleItem(item)}
														aria-label={`Import ${item.label}`}
													/>
													<span class="checkmark"><Check /></span>
												</label>
												<div class="min-w-0 flex-1">
													<div class="flex flex-wrap items-center gap-2">
														<span class="item-name">{item.label}</span>
														<span class="provenance-chip {item.provider ?? 'unknown'}">{item.provenance}</span>
													<span class="status-chip {statusKind(item)}">{statusLabel(item)}</span>
													</div>
													{#if item.description}<p class="item-description">{item.description}</p>{/if}
													{#if item.source_display_path || item.destination_display_path}
														<p class="item-path">{item.source_display_path || 'Source'} <ArrowRight /> {item.destination_display_path || 'Managed destination'}</p>
													{/if}
													{#if item.conflict}<p class="item-conflict">{item.conflict}</p>{/if}
													{#each item.diagnostics as diagnostic}<p class="item-diagnostic">{diagnostic.message}</p>{/each}
												</div>
											</li>
										{/each}
									</ul>
								</section>
							{/each}
						</div>
					{/if}

					{#if preview.diagnostics.length > 0}
						<div class="diagnostics-panel">
							<h3>Scan notes</h3>
							<ul>{#each preview.diagnostics as diagnostic}<li class:error-note={diagnostic.severity === 'error'}>{diagnostic.message}</li>{/each}</ul>
						</div>
					{/if}
				</div>
			{:else if step === 'confirm' && preview}
				<div class="mx-auto max-w-xl space-y-5">
					<button type="button" class="back-button" onclick={backToPreview} disabled={applying}><ArrowLeft />Back to selection</button>
					<div>
						<h3 class="text-lg font-semibold text-foreground">Confirm this import</h3>
						<p class="mt-1 text-sm leading-relaxed text-muted-foreground">{selectedItems.length} selected {selectedItems.length === 1 ? 'item will' : 'items will'} be copied from the previewed source.</p>
					</div>

					<div class="route-panel compact">
						<code>{preview.source_display_path || 'Detected source'}</code>
						<ArrowRight aria-hidden="true" />
						<code>{preview.destination_display_path || 'Managed Fluid settings'}</code>
					</div>

					<ul class="confirm-list">
						{#each selectedItems as item (item.id)}
							<li><Check aria-hidden="true" /><span>{item.label}</span><small>{item.provenance}</small></li>
						{/each}
					</ul>

					<label class="confirm-check">
						<input type="checkbox" bind:checked={confirmCopy} disabled={applying} />
						<span class="checkmark"><Check /></span>
						<span>I understand the provider sources stay untouched, existing Fluid settings win, and destination files will not be overwritten.</span>
					</label>

					{#if error}<div class="error-panel" role="alert"><AlertTriangle /><p>{error}</p></div>{/if}

					<div class="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
						<button type="button" class="secondary-button" onclick={backToPreview} disabled={applying}>Cancel</button>
						<button type="button" class="primary-button" onclick={applyImport} disabled={!confirmCopy || applying}>
							{#if applying}<LoaderCircle class="animate-spin" />Importing…{:else}<FileInput />Import selected{/if}
						</button>
					</div>
				</div>
			{:else if step === 'result' && result}
				<div class="mx-auto max-w-2xl space-y-5">
					<div class="result-hero">
						<div><Check aria-hidden="true" /></div>
						<h3>Import finished</h3>
						<p>{result.imported.length} imported · {result.skipped.length} skipped · {result.conflicts.length} conflicts · {result.errors.length} errors</p>
					</div>

					<div class="grid gap-3 sm:grid-cols-2">
						{#each ['imported', 'skipped', 'conflicts', 'errors'] as group}
							{@const typedGroup = group as 'imported' | 'skipped' | 'conflicts' | 'errors'}
							<section class="outcome-card {typedGroup}">
								<div><h4>{outcomeTitle(typedGroup)}</h4><span>{outcomeHint(typedGroup)}</span></div>
								<strong>{outcomeItems(typedGroup).length}</strong>
								{#if outcomeItems(typedGroup).length > 0}
									<ul>{#each outcomeItems(typedGroup) as item}<li><span>{item.label}</span>{#if item.message}<small>{item.message}</small>{/if}</li>{/each}</ul>
								{/if}
							</section>
						{/each}
					</div>

					{#if result.imported_projects.length > 0}
						<div class="info-panel"><RefreshCw aria-hidden="true" /><p><strong>Projects refreshed.</strong> {result.imported_projects.length} imported {result.imported_projects.length === 1 ? 'project is' : 'projects are'} now available.</p></div>
					{/if}

					<div class="flex justify-end"><button type="button" class="primary-button" onclick={requestClose}>Done</button></div>
				</div>
			{/if}
		</div>

		{#if step === 'preview' && preview}
			<footer class="flex flex-col gap-3 border-t border-border bg-muted/30 px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-6">
				<p class="text-xs text-muted-foreground">Preview complete. No files change until you review and apply this selection.</p>
				<button type="button" class="primary-button" onclick={openConfirmation} disabled={selectedItems.length === 0}>
					Review import <ArrowRight />
				</button>
			</footer>
		{/if}
	</div>
</div>

<style>
	.progress {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 10px 20px;
		border-bottom: 1px solid var(--border);
		background: color-mix(in oklch, var(--muted) 35%, transparent);
		font-size: 11px;
		color: var(--muted-foreground);
	}
	.progress :global(svg) { width: 12px; height: 12px; opacity: 0.55; }
	.progress span.active { color: var(--foreground); font-weight: 600; }
	.progress span.done { color: var(--primary); }

	.safety-panel, .error-panel, .info-panel, .warning-panel {
		display: flex;
		align-items: flex-start;
		gap: 12px;
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 14px;
	}
	.safety-panel { background: color-mix(in oklch, var(--primary) 7%, var(--card)); border-color: color-mix(in oklch, var(--primary) 26%, var(--border)); }
	.safety-panel :global(svg), .info-panel :global(svg), .warning-panel :global(svg), .error-panel :global(svg) { width: 18px; height: 18px; flex: none; margin-top: 1px; }
	.safety-panel :global(svg), .info-panel :global(svg) { color: var(--primary); }
	.safety-panel h3 { margin: 0; font-size: 13px; font-weight: 650; color: var(--foreground); }
	.safety-panel p, .info-panel p, .warning-panel p, .error-panel p { margin: 3px 0 0; font-size: 12.5px; line-height: 1.55; color: var(--muted-foreground); }
	.info-panel { background: var(--muted); }
	.warning-panel { border-color: color-mix(in oklch, #e6a23c 38%, var(--border)); background: color-mix(in oklch, #e6a23c 8%, var(--card)); }
	.warning-panel :global(svg) { color: #d59028; }
	.warning-panel ul { margin: 8px 0 0 18px; list-style: disc; font-size: 12px; color: var(--muted-foreground); }
	.error-panel { border-color: color-mix(in oklch, var(--destructive) 40%, var(--border)); background: color-mix(in oklch, var(--destructive) 8%, var(--card)); }
	.error-panel :global(svg), .error-panel p { color: var(--destructive); }
	.error-panel span { display: block; margin-top: 3px; font-size: 11.5px; color: var(--muted-foreground); }

	.provider-fieldset { margin: 0; min-width: 0; border: 0; padding: 0; }
	.provider-fieldset legend { margin-bottom: 8px; font-size: 13px; font-weight: 600; color: var(--foreground); }
	.provider-grid { display: grid; grid-template-columns: 1.35fr 1fr 1fr; gap: 8px; }
	.provider-card {
		display: flex; min-width: 0; min-height: 86px; align-items: flex-start; gap: 10px;
		border: 1px solid var(--border); border-radius: 10px; padding: 12px;
		background: var(--background); cursor: pointer;
		transition: border-color 180ms ease, background 180ms ease, transform 120ms ease;
	}
	.provider-card:hover { border-color: color-mix(in oklch, var(--primary) 38%, var(--border)); background: color-mix(in oklch, var(--primary) 3%, var(--background)); }
	.provider-card:active { transform: translateY(1px); }
	.provider-card:has(input:checked) { border-color: color-mix(in oklch, var(--primary) 62%, var(--border)); background: color-mix(in oklch, var(--primary) 8%, var(--card)); box-shadow: inset 0 0 0 1px color-mix(in oklch, var(--primary) 15%, transparent); }
	.provider-card:has(input:focus-visible) { outline: 3px solid color-mix(in oklch, var(--ring) 24%, transparent); outline-offset: 2px; }
	.provider-card input { position: absolute; width: 1px; height: 1px; opacity: 0; }
	.provider-card > span:last-child { min-width: 0; }
	.provider-card strong { display: block; font-size: 12.5px; font-weight: 650; color: var(--foreground); }
	.provider-card small { display: block; margin-top: 3px; font-size: 10.5px; line-height: 1.4; color: var(--muted-foreground); }
	.provider-mark { display: grid; flex: none; place-items: center; width: 27px; height: 27px; border-radius: 7px; background: var(--muted); font: 650 9px var(--font-mono, monospace); letter-spacing: -.02em; color: var(--muted-foreground); }
	.provider-card:has(input:checked) .provider-mark { background: var(--primary); color: var(--primary-foreground); }
	.provider-fieldset:disabled .provider-card { cursor: not-allowed; opacity: .6; }

	.source-input {
		width: 100%; min-height: 44px; border: 1px solid var(--border); border-radius: 9px;
		background: var(--background); padding: 10px 12px 10px 38px; color: var(--foreground);
		font-family: var(--font-mono, monospace); font-size: 12.5px; outline: none;
		transition: border-color 180ms ease, box-shadow 180ms ease;
	}
	.source-input:hover { border-color: var(--border-strong, var(--border)); }
	.source-input:focus-visible { border-color: var(--ring); box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 18%, transparent); }
	.source-input:disabled { cursor: not-allowed; opacity: 0.55; }
	code { font-family: var(--font-mono, monospace); font-size: 11.5px; }

	.primary-button, .secondary-button, .back-button {
		display: inline-flex; min-height: 44px; align-items: center; justify-content: center; gap: 7px;
		border-radius: 8px; padding: 9px 14px; font-size: 12.5px; font-weight: 600;
		transition: background 180ms ease, color 180ms ease, border-color 180ms ease, transform 120ms ease;
	}
	.primary-button { border: 1px solid var(--primary); background: var(--primary); color: var(--primary-foreground); }
	.primary-button:hover:not(:disabled) { background: color-mix(in oklch, var(--primary) 88%, var(--foreground)); }
	.secondary-button { border: 1px solid var(--border); background: var(--card); color: var(--muted-foreground); }
	.secondary-button:hover:not(:disabled), .back-button:hover:not(:disabled) { background: var(--muted); color: var(--foreground); }
	.back-button { border: 0; background: transparent; color: var(--muted-foreground); padding-inline: 4px 10px; }
	.primary-button:active:not(:disabled), .secondary-button:active:not(:disabled) { transform: translateY(1px); }
	.primary-button:focus-visible, .secondary-button:focus-visible, .back-button:focus-visible { outline: none; box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 22%, transparent); }
	.primary-button:disabled, .secondary-button:disabled, .back-button:disabled { cursor: not-allowed; opacity: 0.5; }
	.primary-button :global(svg), .secondary-button :global(svg), .back-button :global(svg) { width: 15px; height: 15px; }

	.route-panel {
		display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; gap: 14px;
		border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; background: var(--muted);
	}
	.route-panel > div { min-width: 0; }
	.route-panel span { display: block; margin-bottom: 5px; font-size: 10.5px; font-weight: 650; letter-spacing: .04em; text-transform: uppercase; color: var(--muted-foreground); }
	.route-panel code { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--foreground); }
	.route-panel > :global(svg) { width: 16px; height: 16px; color: var(--primary); }
	.route-panel.compact { grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); }

	.category-card { overflow: hidden; border: 1px solid var(--border); border-radius: 10px; background: var(--background); }
	.category-head { display: flex; align-items: center; gap: 11px; padding: 12px 14px; border-bottom: 1px solid var(--border); background: color-mix(in oklch, var(--muted) 55%, transparent); }
	.category-head h4 { margin: 0; font-size: 13px; font-weight: 650; color: var(--foreground); }
	.category-head p { margin: 2px 0 0; font-size: 11.5px; color: var(--muted-foreground); }
	.count-chip { min-width: 24px; border: 1px solid var(--border); border-radius: 999px; padding: 2px 7px; text-align: center; font: 600 10.5px var(--font-mono, monospace); color: var(--muted-foreground); }

	.category-toggle, .item-toggle, .confirm-check { position: relative; }
	.category-toggle, .item-toggle { display: grid; flex: none; place-items: center; width: 28px; height: 28px; cursor: pointer; }
	.category-toggle input, .item-toggle input, .confirm-check input { position: absolute; width: 1px; height: 1px; opacity: 0; }
	.checkmark { display: grid; place-items: center; width: 18px; height: 18px; border: 1px solid var(--border-strong, var(--border)); border-radius: 5px; background: var(--card); color: transparent; transition: 160ms ease; }
	.checkmark :global(svg) { width: 12px; height: 12px; stroke-width: 3; }
	input:checked + .checkmark { border-color: var(--primary); background: var(--primary); color: var(--primary-foreground); }
	input:focus-visible + .checkmark { outline: 3px solid color-mix(in oklch, var(--ring) 24%, transparent); outline-offset: 2px; }
	input:disabled + .checkmark { cursor: not-allowed; opacity: 0.45; }

	.item-list > li { display: flex; align-items: flex-start; gap: 10px; padding: 12px 14px; }
	.item-list > li + li { border-top: 1px solid var(--border); }
	.item-list > li.unavailable { background: color-mix(in oklch, var(--muted) 45%, transparent); }
	.item-name { font-size: 12.5px; font-weight: 550; color: var(--foreground); }
	.item-description, .item-conflict, .item-diagnostic { margin: 4px 0 0; font-size: 11.5px; line-height: 1.45; color: var(--muted-foreground); }
	.item-conflict { color: #c98220; }
	.item-diagnostic::before { content: 'Note: '; font-weight: 600; }
	.item-path { display: flex; align-items: center; gap: 6px; margin: 6px 0 0; font: 10.5px var(--font-mono, monospace); color: var(--muted-foreground); overflow-wrap: anywhere; }
	.item-path :global(svg) { width: 11px; height: 11px; flex: none; }
	.inventory-note { color: var(--foreground) !important; }
	.provenance-chip, .status-chip { border-radius: 999px; padding: 2px 7px; font-size: 9.5px; font-weight: 650; letter-spacing: .025em; text-transform: uppercase; }
	.provenance-chip { border: 1px solid var(--border); background: var(--card); font-family: var(--font-mono, monospace); color: var(--muted-foreground); }
	.provenance-chip.claude { border-color: color-mix(in oklch, var(--primary) 24%, var(--border)); color: var(--primary); }
	.provenance-chip.codex { border-color: color-mix(in oklch, var(--foreground) 22%, var(--border)); color: var(--foreground); }
	.status-chip { border-radius: 999px; padding: 2px 7px; font-size: 9.5px; font-weight: 650; letter-spacing: .025em; text-transform: uppercase; }
	.status-chip.ready { background: color-mix(in oklch, var(--primary) 13%, transparent); color: var(--primary); }
	.status-chip.warning { background: color-mix(in oklch, #e6a23c 15%, transparent); color: #c98220; }
	.status-chip.muted { background: var(--muted); color: var(--muted-foreground); }

	.empty-panel { display: grid; justify-items: center; border: 1px dashed var(--border); border-radius: 10px; padding: 36px 20px; text-align: center; }
	.empty-panel :global(svg) { width: 28px; height: 28px; color: var(--muted-foreground); }
	.empty-panel h3 { margin: 10px 0 0; font-size: 13px; font-weight: 650; color: var(--foreground); }
	.empty-panel p { margin: 4px 0 0; font-size: 12px; color: var(--muted-foreground); }
	.diagnostics-panel { border-left: 3px solid var(--border-strong, var(--border)); padding-left: 12px; }
	.diagnostics-panel h3 { margin: 0 0 5px; font-size: 11.5px; font-weight: 650; color: var(--foreground); }
	.diagnostics-panel ul { display: grid; gap: 3px; font-size: 11.5px; color: var(--muted-foreground); }
	.diagnostics-panel li.error-note { color: var(--destructive); }

	.confirm-list { max-height: 220px; overflow-y: auto; border: 1px solid var(--border); border-radius: 10px; }
	.confirm-list li { display: flex; align-items: center; gap: 9px; padding: 10px 12px; font-size: 12.5px; color: var(--foreground); }
	.confirm-list li + li { border-top: 1px solid var(--border); }
	.confirm-list :global(svg) { width: 14px; height: 14px; color: var(--primary); }
	.confirm-list li > span { min-width: 0; flex: 1; }
	.confirm-list li > small { font: 600 9.5px var(--font-mono, monospace); text-transform: uppercase; color: var(--muted-foreground); }
	.confirm-check { display: flex; align-items: flex-start; gap: 11px; cursor: pointer; border: 1px solid var(--border); border-radius: 10px; padding: 13px; background: var(--muted); font-size: 12px; line-height: 1.5; color: var(--foreground); }
	.confirm-check .checkmark { flex: none; margin-top: 1px; }

	.result-hero { text-align: center; }
	.result-hero > div { display: grid; place-items: center; width: 42px; height: 42px; margin: 0 auto; border-radius: 50%; background: color-mix(in oklch, var(--primary) 14%, transparent); color: var(--primary); }
	.result-hero :global(svg) { width: 20px; height: 20px; stroke-width: 2.5; }
	.result-hero h3 { margin: 10px 0 0; font-size: 18px; font-weight: 650; color: var(--foreground); }
	.result-hero p { margin: 4px 0 0; font-size: 12px; color: var(--muted-foreground); }
	.outcome-card { position: relative; min-height: 78px; border: 1px solid var(--border); border-radius: 10px; padding: 13px; background: var(--background); }
	.outcome-card > div { padding-right: 40px; }
	.outcome-card h4 { margin: 0; font-size: 12.5px; font-weight: 650; color: var(--foreground); }
	.outcome-card > div > span { display: block; margin-top: 2px; font-size: 10.5px; color: var(--muted-foreground); }
	.outcome-card > strong { position: absolute; right: 13px; top: 13px; font: 650 18px var(--font-mono, monospace); color: var(--foreground); }
	.outcome-card ul { display: grid; gap: 7px; margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--border); }
	.outcome-card li span { display: block; font-size: 11.5px; color: var(--foreground); }
	.outcome-card li small { display: block; margin-top: 1px; font-size: 10.5px; color: var(--muted-foreground); }
	.outcome-card.conflicts, .outcome-card.errors { border-color: color-mix(in oklch, var(--destructive) 25%, var(--border)); }

	@media (max-width: 640px) {
		.progress { justify-content: flex-start; overflow-x: auto; }
		.progress span { white-space: nowrap; }
		.provider-grid { grid-template-columns: 1fr; }
		.provider-card { min-height: 72px; }
		.route-panel { grid-template-columns: 1fr; gap: 8px; }
		.route-panel > :global(svg) { transform: rotate(90deg); justify-self: center; }
		.route-panel code { white-space: normal; overflow-wrap: anywhere; }
		.route-panel.compact { grid-template-columns: 1fr; text-align: center; }
	}

	@media (prefers-reduced-motion: reduce) {
		.primary-button, .secondary-button, .back-button, .provider-card, .source-input, .checkmark { transition: none; }
		:global(.animate-spin) { animation: none; }
	}
</style>
