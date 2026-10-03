<script lang="ts" module>
	/** Spec-line numbers for one agent row. */
	export interface AgentSpec {
		tools: number;
		agents: number;
	}

	/** A delete or restore waiting for the inline confirm in a row header. */
	export interface PendingConfirm {
		id: string;
		kind: 'delete' | 'revert';
	}
</script>

<script lang="ts">
	/**
	 * AgentList: the Agents ledger. One searchable list, grouped by folder, where
	 * each agent opens in place into its editor (the `editor` snippet, owned by
	 * ProfileCard). The open row's header sticks while the editor scrolls and
	 * keeps showing the live spec line and save state. Clicking a segment of a
	 * spec line ("12 tools") opens the agent straight to that section. Delete
	 * and restore confirm inline: the row header turns into the question.
	 */
	import type { Snippet } from 'svelte';
	import type { Profile } from '$lib/api/client';
	import { ChevronDown, Download, Plus, RotateCcw, Search, Sparkles, Trash2, Upload } from 'lucide-svelte';
	import AgentAvatar from '$lib/components/assistant/AgentAvatar.svelte';
	import { agentIdentities, assistantState } from '$lib/stores/assistant';

	interface GroupState {
		profiles?: {
			groups?: { name: string }[];
			assignments?: Record<string, string>;
		};
	}

	interface Props {
		profiles: Profile[];
		groups: GroupState;
		/** The open agent's id; '' while a new agent is being drafted; null for none. */
		selectedId: string | null;
		/** Name typed into the new-agent draft, shown on its row. */
		draftName?: string;
		importing?: boolean;
		loading?: boolean;
		saveState?: 'idle' | 'saving' | 'saved' | 'error';
		confirm?: PendingConfirm | null;
		/** Spec-line numbers; ProfileCard answers live for the open agent. */
		getSpec: (profile: Profile) => AgentSpec;
		/** Open (or re-focus) an agent, optionally straight to one section. */
		onSelect: (profile: Profile, section?: string) => void;
		onClose: () => void;
		onNew: () => void;
		onGenerate: () => void;
		onImport: () => void;
		onExport: (profile: Profile) => void;
		onAskDelete: (profile: Profile) => void;
		onAskRevert: (profile: Profile) => void;
		onConfirm: () => void;
		onCancelConfirm: () => void;
		/** The open agent's editor, rendered under its header row. */
		editor: Snippet;
	}

	let {
		profiles,
		groups,
		selectedId,
		draftName = '',
		importing = false,
		loading = false,
		saveState = 'idle',
		confirm = null,
		getSpec,
		onSelect,
		onClose,
		onNew,
		onGenerate,
		onImport,
		onExport,
		onAskDelete,
		onAskRevert,
		onConfirm,
		onCancelConfirm,
		editor
	}: Props = $props();

	let query = $state('');
	const q = $derived(query.trim().toLowerCase());

	// The open agent always stays in view, even when the search no longer matches it.
	const filtered = $derived(
		q
			? profiles.filter(
					(p) =>
						p.id === selectedId ||
						[p.name, p.description, p.id, groups?.profiles?.assignments?.[p.id]]
							.filter(Boolean)
							.join(' ')
							.toLowerCase()
							.includes(q)
				)
			: profiles
	);
	const matchCount = $derived(filtered.filter((p) => p.id !== selectedId || !q).length);

	/** Folder sections in the user's group order, then ungrouped agents last. */
	const sections = $derived.by(() => {
		const assignments = groups?.profiles?.assignments ?? {};
		const order = (groups?.profiles?.groups ?? []).map((g) => g.name);
		const byGroup = new Map<string, Profile[]>();
		for (const p of filtered) {
			const g = assignments[p.id] && order.includes(assignments[p.id]) ? assignments[p.id] : '';
			byGroup.set(g, [...(byGroup.get(g) ?? []), p]);
		}
		const named = order.filter((g) => byGroup.has(g)).map((g) => ({ label: g, items: byGroup.get(g)! }));
		const rest = byGroup.get('') ?? [];
		if (!rest.length) return named;
		return [...named, { label: named.length ? 'Ungrouped' : '', items: rest }];
	});

	function avatarOf(p: Profile) {
		return ($agentIdentities.find((a) => a.id === p.id)?.avatar_id ?? 'robot') as never;
	}

	/** Row click: a spec segment opens its section; anywhere else toggles the row. */
	function activate(event: MouseEvent, p: Profile) {
		const section = (event.target as Element | null)?.closest?.('[data-sec]')?.getAttribute('data-sec');
		if (section) onSelect(p, section);
		else if (selectedId === p.id) onClose();
		else onSelect(p);
	}

	const SAVE_LABELS = { idle: '', saving: 'Saving…', saved: 'Saved', error: 'Not saved' } as const;

	function focusOnMount(node: HTMLElement) {
		node.focus();
	}
</script>

<div class="al">
	<div class="al-inner">
		<header class="al-head">
			<h1 class="al-title">Agents <span class="al-count">{profiles.length}</span></h1>
			<label class="al-search">
				<Search size={14} strokeWidth={1.8} />
				<input type="search" placeholder="Search agents…" autocomplete="off" aria-label="Search agents" bind:value={query} />
			</label>
			<div class="al-tools">
				<button type="button" class="al-ibtn" title="Import .json" aria-label="Import agent" disabled={importing} onclick={onImport}>
					<Upload size={15} strokeWidth={1.8} />
				</button>
				<button type="button" class="al-ibtn" title="Generate with AI" aria-label="Generate agent with AI" onclick={onGenerate}>
					<Sparkles size={15} strokeWidth={1.8} />
				</button>
				<button type="button" class="al-new" onclick={onNew} aria-label="New agent">
					<Plus size={15} strokeWidth={2.2} /><span>New agent</span>
				</button>
			</div>
		</header>

		<div class="al-list">
			{#if selectedId === ''}
				<div class="lr open draft" data-agent-row="">
					<div class="lr-head">
						<div class="lr-main static">
							<AgentAvatar avatar="robot" size={30} />
							<span class="lr-text">
								<span class="lr-name"><span class="lr-name-t">{draftName.trim() || 'New agent'}</span></span>
								<span class="lr-spec">Name it to save</span>
							</span>
						</div>
						<span class="lr-save" role="status" aria-live="polite">{SAVE_LABELS[saveState]}</span>
					</div>
					<div class="lr-body">{@render editor()}</div>
				</div>
			{/if}

			{#each sections as section (section.label)}
				{#if section.label}
					<div class="al-label">{section.label} <span class="al-label-n">{section.items.length}</span></div>
				{/if}
				{#each section.items as p (p.id)}
					{@const open = selectedId === p.id}
					{@const spec = getSpec(p)}
					{@const pending = confirm?.id === p.id ? confirm : null}
					<div class="lr" class:open data-agent-row={p.id}>
						{#if pending}
							<div class="lr-head confirm" role="group" aria-label={pending.kind === 'delete' ? 'Confirm delete' : 'Confirm restore'}>
								<span class="lr-cmsg">
									{#if pending.kind === 'delete'}
										Delete <strong>{p.name}</strong>? This can't be undone.
									{:else}
										Restore <strong>{p.name}</strong> to its defaults? Your changes are lost.
									{/if}
								</span>
								<button type="button" class="lr-btn" onclick={onCancelConfirm}>Cancel</button>
								<button type="button" class="lr-btn danger" use:focusOnMount onclick={onConfirm}>
									{pending.kind === 'delete' ? 'Delete' : 'Restore'}
								</button>
							</div>
						{:else}
							<div class="lr-head">
								<button
									type="button"
									class="lr-main"
									aria-expanded={open}
									title={open ? undefined : p.description || undefined}
									onclick={(e) => activate(e, p)}
								>
									<AgentAvatar avatar={avatarOf(p)} size={30} />
									<span class="lr-text">
										<span class="lr-name">
											<span class="lr-name-t">{p.name}</span>
											{#if $assistantState?.agent_id === p.id}<span class="lr-pill teal">Assistant</span>{/if}
											{#if p.is_builtin}
												<span class="lr-pill">Built-in</span>
												{#if p.is_modified}<span class="lr-pill warn">Modified</span>{/if}
											{/if}
										</span>
										<span class="lr-spec">
											<span class="lr-seg" data-sec="tools">{spec.tools} tool{spec.tools === 1 ? '' : 's'}</span>
											<span class="lr-dot" aria-hidden="true">·</span>
											<span class="lr-seg" data-sec="subagents">{spec.agents === 0 ? 'No subagents' : `${spec.agents} agent${spec.agents === 1 ? '' : 's'}`}</span>
										</span>
									</span>
									{#if p.description && !open}<span class="lr-desc">{p.description}</span>{/if}
								</button>
								{#if open}
									<span class="lr-save" role="status" aria-live="polite">{SAVE_LABELS[saveState]}</span>
								{:else}
									<span class="lr-acts">
										<button type="button" class="lr-ib" title="Export .json" aria-label={`Export ${p.name}`} onclick={() => onExport(p)}>
											<Download size={14} strokeWidth={1.8} />
										</button>
										{#if p.is_builtin && p.is_modified}
											<button type="button" class="lr-ib" title="Restore defaults" aria-label={`Restore ${p.name} defaults`} onclick={() => onAskRevert(p)}>
												<RotateCcw size={14} strokeWidth={1.8} />
											</button>
										{/if}
										{#if !p.is_builtin}
											<button type="button" class="lr-ib danger" title="Delete" aria-label={`Delete ${p.name}`} onclick={() => onAskDelete(p)}>
												<Trash2 size={14} strokeWidth={1.8} />
											</button>
										{/if}
									</span>
								{/if}
								<span class="lr-chev" aria-hidden="true"><ChevronDown size={16} strokeWidth={1.8} /></span>
							</div>
						{/if}
						{#if open}
							<div class="lr-body">{@render editor()}</div>
						{/if}
					</div>
				{/each}
			{/each}

			{#if loading && profiles.length === 0}
				<p class="al-empty" role="status">Loading agents…</p>
			{:else if q && matchCount === 0}
				<p class="al-empty">
					No agents match “{query.trim()}”.
					<button type="button" class="al-clear" onclick={() => (query = '')}>Clear search</button>
				</p>
			{:else if profiles.length === 0 && selectedId !== ''}
				<p class="al-empty">
					No agents yet.
					<button type="button" class="al-clear" onclick={onNew}>Make one</button>
				</p>
			{/if}
		</div>
	</div>
</div>

<style>
	.al {
		container: al / inline-size;
		height: 100%;
		min-height: 0;
		overflow-y: auto;
	}
	/* Keep scrolled-to rows and sections clear of the sticky toolbar and row header. */
	.lr {
		scroll-margin-top: 66px;
	}
	.lr-body :global(.dx) {
		scroll-margin-top: 126px;
	}
	.al-inner {
		max-width: 1120px;
		margin-inline: auto;
		padding: 0 28px 64px;
	}

	/* ── Toolbar ── */
	.al-head {
		position: sticky;
		top: 0;
		z-index: 4;
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 18px 0 12px;
		background: var(--background);
	}
	.al-title {
		display: flex;
		align-items: baseline;
		gap: 8px;
		margin: 0;
		font-family: var(--font-display, Georgia, serif);
		font-size: 1.3rem;
		font-weight: 700;
		letter-spacing: -0.01em;
		color: var(--foreground);
		white-space: nowrap;
	}
	.al-count {
		font-family: var(--font-mono, monospace);
		font-size: 0.72rem;
		font-weight: 500;
		color: var(--muted-foreground);
	}
	.al-search {
		position: relative;
		flex: 1;
		max-width: 360px;
		margin-left: auto;
		display: flex;
		align-items: center;
	}
	.al-search :global(svg) {
		position: absolute;
		left: 10px;
		color: var(--muted-foreground);
		pointer-events: none;
	}
	.al-search input {
		width: 100%;
		height: 32px;
		padding: 0 10px 0 30px;
		background: var(--input-bg, var(--background));
		border: 1px solid var(--border);
		border-radius: 8px;
		color: var(--foreground);
		font-size: 0.8125rem;
		outline: none;
	}
	.al-search input:focus {
		border-color: var(--ring);
	}
	.al-tools {
		display: flex;
		align-items: center;
		gap: 4px;
	}
	.al-ibtn {
		display: grid;
		place-items: center;
		width: 32px;
		height: 32px;
		border: 1px solid transparent;
		border-radius: 8px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
	}
	.al-ibtn:hover {
		color: var(--foreground);
		background: var(--hover-overlay, var(--muted));
	}
	.al-ibtn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.al-new {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		height: 32px;
		padding: 0 12px 0 10px;
		border: none;
		border-radius: 8px;
		background: var(--primary);
		color: var(--primary-foreground);
		font-size: 0.8rem;
		font-weight: 600;
		cursor: pointer;
		white-space: nowrap;
	}
	.al-new:hover {
		filter: brightness(1.06);
	}

	/* ── Group labels ── */
	.al-label {
		display: flex;
		align-items: baseline;
		gap: 6px;
		padding: 18px 12px 6px;
		font-size: 12px;
		font-weight: 600;
		color: var(--muted-foreground);
	}
	.al-label-n {
		font-family: var(--font-mono, monospace);
		font-size: 11px;
		font-weight: 400;
		opacity: 0.8;
	}

	/* ── Rows ── */
	.lr {
		border-radius: 10px;
	}
	.lr + .lr:not(.open) .lr-head,
	.lr.open + .lr .lr-head {
		border-top: 1px solid color-mix(in oklch, var(--border) 60%, transparent);
	}
	.lr.open {
		margin: 6px 0;
		background: color-mix(in oklch, var(--primary) 4%, var(--background));
		box-shadow: inset 2px 0 0 var(--primary);
	}
	.lr.open + .lr .lr-head {
		border-top: none;
	}
	.lr-head {
		position: relative;
		display: flex;
		align-items: center;
		gap: 10px;
		min-height: 56px;
		padding: 0 12px;
		border-radius: 10px;
	}
	.lr:not(.open) .lr-head:hover {
		background: var(--hover-overlay, color-mix(in oklch, var(--muted) 50%, transparent));
	}
	.lr.open .lr-head {
		position: sticky;
		top: 62px;
		z-index: 3;
		border-radius: 10px 10px 0 0;
		background: color-mix(in oklch, var(--primary) 4%, var(--background));
		box-shadow:
			inset 2px 0 0 var(--primary),
			0 1px 0 color-mix(in oklch, var(--border) 70%, transparent);
	}
	.lr-main {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 10px 0;
		border: none;
		background: none;
		font: inherit;
		color: var(--foreground);
		text-align: left;
		cursor: pointer;
	}
	/* The whole header row is the hit target; actions and spec segments sit above it. */
	.lr-main:not(.static)::after {
		content: '';
		position: absolute;
		inset: 0;
		border-radius: inherit;
	}
	.lr-main.static {
		cursor: default;
	}
	/* A fixed name column so descriptions line up across rows. */
	.lr-text {
		flex: 0 0 280px;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.lr-name {
		display: flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
		font-size: 14px;
		font-weight: 550;
	}
	.lr-name-t {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.lr-pill {
		flex-shrink: 0;
		padding: 1px 6px;
		border-radius: 999px;
		background: color-mix(in oklch, var(--muted) 70%, transparent);
		color: var(--muted-foreground);
		font-size: 10.5px;
		font-weight: 600;
	}
	.lr-pill.teal {
		background: color-mix(in oklch, var(--primary) 14%, transparent);
		color: var(--primary);
	}
	.lr-pill.warn {
		background: color-mix(in oklch, var(--warning, #b7791f) 15%, transparent);
		color: var(--warning, #b7791f);
	}
	.lr-spec {
		display: flex;
		align-items: baseline;
		gap: 6px;
		font-size: 12px;
		color: var(--muted-foreground);
		white-space: nowrap;
	}
	.lr-seg {
		position: relative;
		z-index: 1;
		border-radius: 4px;
	}
	.lr-seg:hover {
		color: var(--primary);
		text-decoration: underline;
		text-underline-offset: 2px;
	}
	.lr-dot {
		opacity: 0.6;
	}
	.lr-desc {
		flex: 1;
		min-width: 0;
		margin-left: 16px;
		font-size: 12.5px;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.lr-save {
		flex-shrink: 0;
		min-width: 4.5em;
		font-size: 12px;
		color: var(--muted-foreground);
		text-align: right;
	}
	.lr-acts {
		position: relative;
		z-index: 1;
		display: flex;
		gap: 2px;
		flex-shrink: 0;
		opacity: 0;
		transition: opacity 0.12s;
	}
	.lr-head:hover .lr-acts,
	.lr-head:focus-within .lr-acts {
		opacity: 1;
	}
	.lr-ib {
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border: none;
		border-radius: 7px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
	}
	.lr-ib:hover {
		color: var(--foreground);
		background: var(--muted);
	}
	.lr-ib.danger:hover {
		color: var(--destructive);
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
	}
	.lr-chev {
		display: grid;
		place-items: center;
		flex-shrink: 0;
		width: 20px;
		color: var(--muted-foreground);
		transition: transform 0.16s;
	}
	.lr.open .lr-chev {
		transform: rotate(180deg);
	}
	.lr-body {
		padding: 8px 20px 4px 54px;
	}

	/* Inline confirm */
	.lr-head.confirm {
		gap: 8px;
		background: color-mix(in oklch, var(--destructive) 8%, var(--background));
	}
	.lr-cmsg {
		flex: 1;
		min-width: 0;
		font-size: 13px;
		color: var(--foreground);
	}
	.lr-btn {
		height: 30px;
		padding: 0 12px;
		border: 1px solid var(--border);
		border-radius: 7px;
		background: var(--background);
		color: var(--foreground);
		font: inherit;
		font-size: 12.5px;
		font-weight: 550;
		cursor: pointer;
	}
	.lr-btn:hover {
		background: var(--muted);
	}
	.lr-btn.danger {
		border-color: transparent;
		background: var(--destructive);
		color: var(--destructive-foreground, white);
	}
	.lr-btn.danger:hover {
		filter: brightness(1.08);
	}

	.al-empty {
		margin: 20px 12px;
		font-size: 13px;
		color: var(--muted-foreground);
	}
	.al-clear {
		margin-left: 6px;
		padding: 0;
		border: none;
		background: none;
		font: inherit;
		font-weight: 600;
		color: var(--primary);
		cursor: pointer;
	}
	.al-clear:hover {
		text-decoration: underline;
	}

	.al-ibtn:focus-visible,
	.al-new:focus-visible,
	.lr-main:focus-visible,
	.lr-ib:focus-visible,
	.lr-btn:focus-visible,
	.al-clear:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}
	.lr-main:focus-visible {
		outline: none;
	}
	.lr-main:focus-visible::after {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	@media (hover: none) {
		.lr-acts {
			display: none;
		}
	}
	@container al (max-width: 900px) {
		.lr-desc {
			display: none;
		}
		.lr-text {
			flex: 1 1 auto;
		}
	}
	@container al (max-width: 600px) {
		.al-inner {
			padding: 0 12px 48px;
		}
		.al-head {
			flex-wrap: wrap;
			gap: 8px;
			padding: 14px 0 10px;
		}
		.al-search {
			order: 3;
			flex-basis: 100%;
			max-width: none;
		}
		.al-tools {
			margin-left: auto;
		}
		.al-new span {
			display: none;
		}
		.al-new {
			width: 32px;
			padding: 0;
			justify-content: center;
		}
		.lr.open .lr-head {
			top: 96px;
		}
		.lr {
			scroll-margin-top: 100px;
		}
		.lr-body :global(.dx) {
			scroll-margin-top: 160px;
		}
		.lr-head {
			padding: 0 10px;
		}
		.lr-acts {
			display: none;
		}
		.lr-body {
			padding: 6px 12px 4px 14px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.lr-chev,
		.lr-acts {
			transition: none;
		}
	}
</style>
