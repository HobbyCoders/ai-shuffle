<script lang="ts" module>
	export type ImportStep = 'source' | 'preview' | 'result';
</script>

<script lang="ts">
	/**
	 * ImportWizard: copy instructions, skills, agents, MCP inventory and
	 * projects from Claude Code and Codex, inline under System > Workspace.
	 * Source, then Preview (choose items and confirm), then Result.
	 *
	 * Ported from SettingsImportDialog. The rules stay: preview is read-only,
	 * sources are never touched, existing destinations win, conflicting or
	 * duplicate items cannot be selected, MCP secrets never appear, and a
	 * stale preview (409 on apply) sends you back to scan again.
	 */
	import { ScanSearch, ChevronRight } from 'lucide-svelte';
	import { getErrorMessage, isApiError } from '$lib/api/client';
	import {
		applySettingsImport,
		previewSettingsImport,
		type SettingsImportApplyResponse,
		type SettingsImportCategory,
		type SettingsImportItem,
		type SettingsImportPreview,
		type SettingsImportProvider
	} from '$lib/api/settings-import';
	import { appData } from '$lib/stores/app-data.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import Segmented from '../../rows/Segmented.svelte';
	import TextField from '../../rows/TextField.svelte';
	import StatusDot from '../../rows/StatusDot.svelte';
	import InlineBanner from '../../rows/InlineBanner.svelte';

	interface Props {
		/** Current step, for the row's value. */
		step?: ImportStep;
		/** Called after an import finishes, with the number of items imported. */
		onImported?: (count: number) => void;
		/** Done on the result step. */
		onClose?: () => void;
	}

	let { step = $bindable('source'), onImported, onClose }: Props = $props();

	type Source = 'both' | SettingsImportProvider;
	const SOURCES: { value: Source; label: string }[] = [
		{ value: 'both', label: 'Claude and Codex' },
		{ value: 'claude', label: 'Claude' },
		{ value: 'codex', label: 'Codex' }
	];

	let source = $state<Source>('both');
	let sourceHome = $state('');
	let preview = $state<SettingsImportPreview | null>(null);
	let result = $state<SettingsImportApplyResponse | null>(null);
	let selected = $state(new Set<string>());
	let open = $state(new Set<string>());
	let scanning = $state(false);
	let applying = $state(false);
	let acknowledged = $state(false);
	let error = $state('');
	let stale = $state(false);
	let live = $state('');

	const providers = $derived<SettingsImportProvider[]>(source === 'both' ? ['codex', 'claude'] : [source]);
	const sourceHint = $derived(source === 'claude' ? '~/.claude' : source === 'codex' ? '~/.codex' : 'Duplicates collapse');
	const items = $derived(preview?.categories.flatMap((c) => c.items) ?? []);
	const chosen = $derived(items.filter((item) => selected.has(item.id)));
	const conflictCount = $derived.by(() => {
		const conflicts = new Set(preview?.conflicts ?? []);
		for (const item of items) {
			if (item.conflict || ['conflict', 'duplicate'].includes(item.status)) conflicts.add(item.conflict || item.id);
		}
		return conflicts.size;
	});

	function unavailable(item: SettingsImportItem): boolean {
		return !item.selectable || !!item.conflict || ['conflict', 'duplicate'].includes(item.status);
	}

	function pickable(category: SettingsImportCategory): SettingsImportItem[] {
		return category.items.filter((item) => !unavailable(item));
	}

	function countIn(category: SettingsImportCategory): number {
		return category.items.filter((item) => selected.has(item.id)).length;
	}

	function allIn(category: SettingsImportCategory): boolean {
		const list = pickable(category);
		return list.length > 0 && list.every((item) => selected.has(item.id));
	}

	function toggleCategory(category: SettingsImportCategory) {
		const on = !allIn(category);
		const next = new Set(selected);
		for (const item of pickable(category)) {
			if (on) next.add(item.id);
			else next.delete(item.id);
		}
		selected = next;
		acknowledged = false;
	}

	function toggleItem(item: SettingsImportItem) {
		if (unavailable(item)) return;
		const next = new Set(selected);
		if (next.has(item.id)) next.delete(item.id);
		else next.add(item.id);
		selected = next;
		acknowledged = false;
	}

	function toggleOpen(id: string) {
		const next = new Set(open);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		open = next;
	}

	function statusLabel(item: SettingsImportItem): string {
		if (item.status === 'duplicate') return 'Duplicate';
		if (item.conflict || item.status === 'conflict') return 'Conflict';
		if (item.status === 'diagnostic') return 'Diagnostic';
		if (item.status === 'blocked') return 'Blocked';
		if (item.status === 'unsupported') return 'Not supported';
		if (!item.selectable) return 'Inventory only';
		return '';
	}

	async function scan() {
		scanning = true;
		error = '';
		stale = false;
		result = null;
		live = 'Scanning. Nothing is being changed.';
		try {
			const next = await previewSettingsImport({
				providers,
				...(sourceHome.trim() ? { source_home: sourceHome.trim() } : {})
			});
			preview = next;
			selected = new Set(
				next.categories
					.flatMap((c) => c.items)
					.filter((item) => item.selectable && item.selected_by_default && !item.conflict)
					.map((item) => item.id)
			);
			open = new Set();
			acknowledged = false;
			step = 'preview';
			live = `Found ${items.length} items, ${selected.size} selected.`;
		} catch (cause) {
			error = getErrorMessage(cause, 'The sources could not be scanned.');
			live = error;
		} finally {
			scanning = false;
		}
	}

	async function apply() {
		if (!preview || !acknowledged || chosen.length === 0) return;
		applying = true;
		error = '';
		live = 'Importing. Existing files will not be overwritten.';
		try {
			result = await applySettingsImport({
				token: preview.token,
				selected_item_ids: chosen.map((item) => item.id),
				providers: preview.providers,
				...(sourceHome.trim() ? { source_home: sourceHome.trim() } : {})
			});
			if (result.imported_projects.length > 0) await appData.loadProjects();
			step = 'result';
			onImported?.(result.imported.length);
			live = `Imported ${result.imported.length}, skipped ${result.skipped.length}, ${result.conflicts.length} conflicts, ${result.errors.length} errors.`;
		} catch (cause) {
			if (isApiError(cause) && cause.status === 409) {
				stale = true;
				preview = null;
				selected = new Set();
				acknowledged = false;
				step = 'source';
				error = 'This preview is out of date. Scan again before importing.';
			} else {
				error = getErrorMessage(cause, 'The selected items could not be imported.');
			}
			live = error;
		} finally {
			applying = false;
		}
	}

	function restart() {
		preview = null;
		result = null;
		selected = new Set();
		acknowledged = false;
		error = '';
		stale = false;
		step = 'source';
	}

	const OUTCOMES = [
		{ key: 'imported', label: 'Imported', tone: 'ok' },
		{ key: 'skipped', label: 'Skipped', tone: 'off' },
		{ key: 'conflicts', label: 'Kept existing', tone: 'warn' },
		{ key: 'errors', label: 'Failed', tone: 'err' }
	] as const;
</script>

<p class="st-vh" aria-live="polite">{live}</p>

{#if step === 'source'}
	<div class="st-xh">Source</div>
	<div class="st-xlist">
		<SettingRow label="Scan" hint={sourceHint} hintMono={source !== 'both'}>
			<Segmented options={SOURCES} value={source} disabled={scanning} onchange={(v) => (source = v)} />
		</SettingRow>
		<SettingRow label="Settings home" hint="Optional, mount read-only">
			<TextField bind:value={sourceHome} placeholder="Detected server home" mono disabled={scanning} />
		</SettingRow>
	</div>
	<p class="st-note">Sources stay untouched. MCP secrets are never shown or copied.</p>
	{#if error}<p class="st-msg err" role="alert">{error}</p>{/if}
	<div class="st-acts">
		<button type="button" class="st-btn primary" disabled={scanning} onclick={scan}>
			<ScanSearch />{scanning ? 'Scanning…' : stale ? 'Scan again' : 'Scan'}
		</button>
	</div>
{:else if step === 'preview' && preview}
	<div class="st-xh">
		Found <span class="n">{items.length} items</span>
		{#if preview.source_display_path}<span class="st-sp"></span><span class="st-mono path">{preview.source_display_path}</span>{/if}
	</div>
	{#if conflictCount > 0}
		<InlineBanner tone="info">{conflictCount} kept as they are: duplicates or existing files</InlineBanner>
	{/if}
	{#if items.length === 0}
		<p class="st-msg">Nothing to import here. Check the settings home, then scan again.</p>
	{:else}
		<div class="st-xlist">
			{#each preview.categories as category (category.id)}
				{@const list = pickable(category)}
				<div class="st-xitem">
					<div class="cat">
						<label class="st-check">
							<input
								type="checkbox"
								checked={allIn(category)}
								indeterminate={countIn(category) > 0 && !allIn(category)}
								disabled={list.length === 0}
								aria-label="Select all {category.label}"
								onchange={() => toggleCategory(category)}
							/>
							<span>{category.label}</span>
							<span class="st-count">{countIn(category)} of {category.items.length}</span>
						</label>
						<button
							type="button"
							class="st-btn ghost icon sm more"
							aria-expanded={open.has(category.id)}
							aria-label="Show {category.label} items"
							onclick={() => toggleOpen(category.id)}><ChevronRight /></button
						>
					</div>
					{#if open.has(category.id)}
						<ul class="items">
							{#if category.description}<li class="desc">{category.description}</li>{/if}
							{#each category.items as item (item.id)}
								<li>
									<label class="st-check">
										<input
											type="checkbox"
											checked={selected.has(item.id)}
											disabled={unavailable(item)}
											aria-label="Import {item.label}"
											onchange={() => toggleItem(item)}
										/>
										<span class="item">
											<span>{item.label}</span>
											{#if item.conflict}<small>{item.conflict}</small>{/if}
											{#each item.diagnostics as d, i (i)}<small>{d.message}</small>{/each}
										</span>
										<span class="st-count">{statusLabel(item) || item.provenance}</span>
									</label>
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
	{#if preview.diagnostics.length > 0}
		<ul class="notes">
			{#each preview.diagnostics as d, i (i)}<li class:err={d.severity === 'error'}>{d.message}</li>{/each}
		</ul>
	{/if}
	<div class="st-xh">Confirm</div>
	<div class="st-xlist">
		<label class="st-check">
			<input type="checkbox" bind:checked={acknowledged} disabled={applying || chosen.length === 0} />
			<span>Sources untouched, existing settings win</span>
			<span></span>
		</label>
	</div>
	{#if error}<p class="st-msg err" role="alert">{error}</p>{/if}
	<div class="st-acts">
		<button type="button" class="st-btn ghost" disabled={applying} onclick={restart}>Back</button>
		<button type="button" class="st-btn primary" disabled={!acknowledged || applying || chosen.length === 0} onclick={apply}>
			{applying ? 'Importing…' : `Import ${chosen.length} ${chosen.length === 1 ? 'item' : 'items'}`}
		</button>
	</div>
{:else if step === 'result' && result}
	<div class="st-xh">Result</div>
	<div class="st-xlist">
		{#each OUTCOMES as o (o.key)}
			{@const list = result[o.key]}
			{#if o.key === 'imported' || list.length > 0}
				<SettingRow label="{o.label} {list.length}" hint={list.slice(0, 3).map((x) => x.label).join(', ') || undefined}>
					{#if o.key === 'imported' && result.imported_projects.length > 0}
						<span class="st-status"><StatusDot tone="ok" />Projects refreshed</span>
					{:else}
						<StatusDot tone={o.tone} />
					{/if}
				</SettingRow>
			{/if}
		{/each}
	</div>
	<div class="st-acts">
		<button
			type="button"
			class="st-btn primary"
			onclick={() => {
				restart();
				onClose?.();
			}}>Done</button
		>
	</div>
{/if}

<style>
	.n {
		color: var(--st-subtle);
		font-variant-numeric: tabular-nums;
	}
	.path {
		font-size: 11.5px;
		color: var(--st-subtle);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 50%;
	}
	.cat {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		align-items: center;
		padding-right: 10px;
	}
	.more :global(svg.lucide) {
		transition: transform 0.15s;
	}
	.more[aria-expanded='true'] :global(svg.lucide) {
		transform: rotate(90deg);
	}
	.items {
		list-style: none;
		margin: 0;
		padding: 0 0 6px 28px;
	}
	.items .st-check {
		padding-top: 6px;
		padding-bottom: 6px;
		min-height: 34px;
		font-size: 13px;
	}
	.items .desc {
		font-size: 12.5px;
		color: var(--st-mfg);
		padding: 0 16px 6px;
	}
	.item {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}
	.item > span {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.item small {
		font-size: 12px;
		color: var(--st-mfg);
	}
	.notes {
		margin: 8px 2px 0;
		padding-left: 18px;
		font-size: 12.5px;
		color: var(--st-mfg);
	}
	.notes .err {
		color: var(--st-destructive);
	}
</style>
