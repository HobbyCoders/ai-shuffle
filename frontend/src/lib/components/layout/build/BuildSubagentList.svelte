<script lang="ts">
	/**
	 * BuildSubagentList — compact rows presentation of subagents for the Build page.
	 *
	 * Header-less by design (BuildPage supplies the heading + sub-nav). Reuses
	 * SubagentCard's data + editor flow; only the list view is new. Theme tokens
	 * only (matches the Studio-v2 mockup's `.listrows` layout, not its palette).
	 */
	import { Bot, Download, Trash2, RotateCcw, Plus, Sparkles, Upload, Search } from 'lucide-svelte';
	import RowActionMenu from '$lib/components/ui/RowActionMenu.svelte';

	interface Subagent {
		id: string;
		name: string;
		description: string;
		prompt: string;
		tools?: string[];
		model?: string;
		is_builtin: boolean;
		is_modified: boolean;
		created_at: string;
		updated_at: string;
	}

	interface Props {
		subagents: Subagent[];
		importing?: boolean;
		getModelDisplay: (model?: string) => string;
		onEdit: (agent: Subagent) => void;
		onCreate: () => void;
		onGenerate: () => void;
		onImport: () => void;
		onExport: (id: string) => void;
		onDelete: (id: string) => Promise<void> | void;
		onRevert: (id: string) => Promise<void> | void;
	}

	let {
		subagents,
		importing = false,
		getModelDisplay,
		onEdit,
		onCreate,
		onGenerate,
		onImport,
		onExport,
		onDelete,
		onRevert
	}: Props = $props();

	let searchQuery = $state('');
	let pending = $state<{ id: string; kind: 'delete' | 'revert' } | null>(null);
	let pendingLoading = $state(false);
	// Touch/narrow only: which row has its collapsed ⋮ action tray expanded.
	// One open at a time; desktop keeps the always-inline hover-revealed rail.
	let actionsOpenId = $state<string | null>(null);

	// RowActionMenu already stops propagation before calling this, so the row's
	// own click (open the editor) never also fires.
	function toggleRowActions(_e: MouseEvent | KeyboardEvent, id: string) {
		actionsOpenId = actionsOpenId === id ? null : id;
	}
	// Any click reaching the window is outside an open tray (tray buttons all stop
	// propagation) — collapse it and disarm any pending confirmation.
	function onWindowClick() {
		if (actionsOpenId !== null) actionsOpenId = null;
		pending = null;
	}
	function onWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			actionsOpenId = null;
			pending = null;
		}
	}

	const filtered = $derived(
		searchQuery.trim()
			? subagents.filter((s) => {
					const q = searchQuery.toLowerCase();
					return (
						s.name.toLowerCase().includes(q) ||
						s.id.toLowerCase().includes(q) ||
						s.description.toLowerCase().includes(q)
					);
				})
			: subagents
	);

	function toolsLabel(s: Subagent): string {
		if (!Array.isArray(s.tools)) return 'all tools';
		if (s.tools.length === 0) return 'no tools';
		return `${s.tools.length} ${s.tools.length === 1 ? 'tool' : 'tools'}`;
	}

	async function confirmPending() {
		if (!pending) return;
		pendingLoading = true;
		try {
			if (pending.kind === 'delete') await onDelete(pending.id);
			else await onRevert(pending.id);
			pending = null;
		} catch (e) {
			console.error('Subagent action failed:', e);
		} finally {
			pendingLoading = false;
		}
	}
</script>

<svelte:window onclick={onWindowClick} onkeydown={onWindowKeydown} />

<div class="bsl">
	<div class="bsl-toolbar">
		<div class="bsl-search">
			<Search size={14} strokeWidth={1.7} />
			<input type="text" placeholder="Search subagents…" autocomplete="off" bind:value={searchQuery} />
		</div>
		<div class="bsl-tools">
			<button class="bsl-btn" onclick={onImport} disabled={importing}>
				<Upload size={14} strokeWidth={1.7} /><span>Import</span>
			</button>
			<button class="bsl-btn" onclick={onGenerate}>
				<Sparkles size={14} strokeWidth={1.7} /><span>Generate</span>
			</button>
		</div>
	</div>

	<div class="bsl-list">
		{#each filtered as agent (agent.id)}
			{#if pending && pending.id === agent.id}
				<div class="bsl-row bsl-row--confirm">
					<p class="bsl-confirm-text">
						{#if pending.kind === 'delete'}
							Delete <strong>{agent.name}</strong>? This can't be undone.
						{:else}
							Restore <strong>{agent.name}</strong> to its built-in defaults?
						{/if}
					</p>
					<div class="bsl-confirm-actions">
						<button class="bsl-btn" onclick={(e) => { e.stopPropagation(); pending = null; }} disabled={pendingLoading}>Cancel</button>
						<button
							class="bsl-btn {pending.kind === 'delete' ? 'bsl-btn--danger' : 'bsl-btn--primary'}"
							onclick={(e) => { e.stopPropagation(); confirmPending(); }}
							disabled={pendingLoading}
						>
							{pending.kind === 'delete' ? 'Delete' : 'Restore'}
						</button>
					</div>
				</div>
			{:else}
				<div
					class="bsl-row"
					role="button"
					tabindex="0"
					onclick={() => onEdit(agent)}
					onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onEdit(agent); } }}
				>
					<span class="bsl-ic"><Bot size={17} strokeWidth={1.7} /></span>
					<div class="bsl-body">
						<h4 class="bsl-name">
							{agent.name}
							{#if agent.is_builtin}<span class="bsl-badge">built-in</span>{/if}
							{#if agent.is_modified}<span class="bsl-badge bsl-badge--mod">modified</span>{/if}
						</h4>
						{#if agent.description}<p class="bsl-desc">{agent.description}</p>{/if}
					</div>
					<div class="bsl-meta">
						<span class="bsl-tag">{getModelDisplay(agent.model)}</span>
						<span class="bsl-tag">{toolsLabel(agent)}</span>
					</div>
					<div class="bsl-actions" role="toolbar" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
						<RowActionMenu
							open={actionsOpenId === agent.id}
							onToggle={(e) => toggleRowActions(e, agent.id)}
							label="Subagent actions"
						>
							{#if agent.is_builtin && agent.is_modified}
								<button class="bsl-act" title="Revert to defaults" onclick={() => { actionsOpenId = null; pending = { id: agent.id, kind: 'revert' }; }}>
									<RotateCcw size={15} strokeWidth={1.7} />
								</button>
							{/if}
							<button class="bsl-act" title="Export" onclick={() => { actionsOpenId = null; onExport(agent.id); }}>
								<Download size={15} strokeWidth={1.7} />
							</button>
							{#if !agent.is_builtin}
								<button class="bsl-act bsl-act--danger" title="Delete" onclick={() => { actionsOpenId = null; pending = { id: agent.id, kind: 'delete' }; }}>
									<Trash2 size={15} strokeWidth={1.7} />
								</button>
							{/if}
						</RowActionMenu>
					</div>
				</div>
			{/if}
		{/each}

		<button class="bsl-new" onclick={onCreate}>
			<span class="bsl-new-plus"><Plus size={16} strokeWidth={2.2} /></span>
			<span>New subagent</span>
		</button>
	</div>

	{#if filtered.length === 0 && searchQuery.trim()}
		<p class="bsl-empty">No subagents match “{searchQuery}”.</p>
	{/if}
</div>

<style>
	.bsl { display: flex; flex-direction: column; gap: 16px; }

	/* Toolbar */
	.bsl-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
	.bsl-search { position: relative; display: flex; align-items: center; flex: 1; max-width: 320px; }
	.bsl-search :global(svg) { position: absolute; left: 11px; color: var(--muted-foreground); pointer-events: none; }
	.bsl-search input {
		width: 100%; height: 34px; padding: 0 12px 0 32px;
		background: var(--input-bg); border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		color: var(--foreground); font-size: 0.8125rem; outline: none;
		transition: border-color 120ms ease, box-shadow 120ms ease;
	}
	.bsl-search input::placeholder { color: var(--muted-foreground); }
	.bsl-search input:focus { border-color: var(--ring); box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 15%, transparent); }
	.bsl-tools { display: flex; align-items: center; gap: 8px; }

	.bsl-btn {
		display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 13px;
		background: var(--card); color: var(--foreground);
		border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) - 2px);
		font-size: 0.8125rem; font-weight: 500; cursor: pointer; white-space: nowrap;
		transition: background-color 120ms ease, border-color 120ms ease, filter 120ms ease;
	}
	.bsl-btn:hover { background: var(--hover-overlay); }
	.bsl-btn:disabled { opacity: 0.5; cursor: not-allowed; }
	.bsl-btn--primary { background: var(--primary); color: var(--primary-foreground); border-color: transparent; }
	.bsl-btn--primary:hover { filter: brightness(1.08); background: var(--primary); }
	.bsl-btn--danger { color: var(--destructive); border-color: color-mix(in oklch, var(--destructive) 35%, var(--border)); }
	.bsl-btn--danger:hover { background: color-mix(in oklch, var(--destructive) 12%, transparent); }

	/* List */
	.bsl-list {
		display: flex; flex-direction: column;
		border: 1px solid var(--border); border-radius: var(--radius, 0.75rem);
		overflow: hidden; background: var(--card);
	}
	.bsl-row {
		display: flex; align-items: center; gap: 14px;
		padding: 13px 16px; min-height: 60px;
		border-bottom: 1px solid var(--border-subtle, var(--border));
		cursor: pointer; transition: background-color 120ms ease;
	}
	.bsl-row:last-child { border-bottom: none; }
	.bsl-row:hover { background: var(--hover-overlay); }
	.bsl-row:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	.bsl-ic {
		display: grid; place-items: center; width: 36px; height: 36px; flex-shrink: 0;
		border-radius: calc(var(--radius, 0.75rem) - 4px);
		background: color-mix(in oklch, var(--primary) 12%, transparent); color: var(--primary);
	}
	.bsl-body { flex: 1; min-width: 0; }
	.bsl-name {
		display: flex; align-items: center; gap: 8px; margin: 0 0 3px;
		font-family: var(--font-display, Georgia, serif);
		font-size: 0.9rem; font-weight: 700; letter-spacing: -0.01em; color: var(--foreground);
		overflow: hidden;
	}
	.bsl-badge {
		flex-shrink: 0; font-size: 0.6rem; font-weight: 600; letter-spacing: 0.03em;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted) 50%, transparent);
		padding: 2px 6px; border-radius: 999px;
	}
	.bsl-badge--mod { color: var(--warning, #d97706); background: color-mix(in oklch, var(--warning, #d97706) 14%, transparent); }
	.bsl-desc {
		margin: 0; font-size: 0.78rem; color: var(--muted-foreground); line-height: 1.4;
		overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
	}

	.bsl-meta { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
	.bsl-tag {
		font-size: 0.6875rem; font-weight: 500; color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted) 45%, transparent);
		padding: 2px 8px; border-radius: 6px; white-space: nowrap;
	}

	/* Fixed width reserves space for the max action count (2: revert/delete + export),
	   so the meta column stays aligned across rows regardless of which buttons a row shows.
	   Reveal is signalled to RowActionMenu through the inherited --ram-reveal custom
	   property (0 hidden / 1 shown); it owns the button opacity + the touch/narrow ⋮
	   collapse + slide-out. */
	.bsl-actions { display: flex; justify-content: flex-end; gap: 2px; flex-shrink: 0; width: 58px; }
	.bsl-row { --ram-reveal: 0; }
	.bsl-row:hover, .bsl-row:focus-within { --ram-reveal: 1; }
	.bsl-act {
		display: grid; place-items: center; width: 28px; height: 28px;
		background: transparent; border: 1px solid transparent; border-radius: 7px;
		color: var(--muted-foreground); cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.bsl-act:hover { color: var(--foreground); background: var(--hover-overlay); }
	.bsl-act--danger:hover { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 12%, transparent); }

	/* Inline confirm row */
	.bsl-row--confirm {
		justify-content: space-between; cursor: default;
		background: color-mix(in oklch, var(--destructive) 6%, var(--card));
	}
	.bsl-confirm-text { margin: 0; font-size: 0.8rem; color: var(--foreground); }
	.bsl-confirm-actions { display: flex; gap: 8px; flex-shrink: 0; }

	/* New row */
	.bsl-new {
		display: flex; align-items: center; justify-content: center; gap: 8px;
		padding: 13px 16px; min-height: 52px;
		background: transparent; border: none; border-top: 1px dashed color-mix(in oklch, var(--muted-foreground) 35%, transparent);
		color: var(--muted-foreground); font-size: 0.8rem; font-weight: 600; cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.bsl-new:hover { color: var(--foreground); background: var(--hover-overlay); }
	.bsl-new-plus {
		display: grid; place-items: center; width: 24px; height: 24px; border-radius: 50%;
		background: var(--primary); color: var(--primary-foreground);
	}

	.bsl-empty { font-size: 0.8rem; color: var(--muted-foreground); text-align: center; padding: 8px 0; }

	@media (max-width: 639px) {
		.bsl-search { max-width: none; }
		.bsl-meta { display: none; }
	}
</style>
