<script lang="ts">
	/**
	 * BuildProfileGrid — card-grid presentation of profiles for the Build page.
	 *
	 * Header-less by design: the Build page supplies the page heading + sub-nav,
	 * so this renders only the grid (no FeaturePageLayout). All data + actions are
	 * passed in by ProfileCard, which still owns loading and the editor flow.
	 *
	 * Styled with the app's existing theme tokens (matches the Studio-v2 mockup
	 * layout, not its palette).
	 */
	import type { Profile } from '$lib/api/client';
	import { Users, Download, Trash2, RotateCcw, Plus, Sparkles, Upload, Search } from 'lucide-svelte';
	import RowActionMenu from '$lib/components/ui/RowActionMenu.svelte';

	interface GroupState {
		profiles?: {
			groups?: { name: string }[];
			assignments?: Record<string, string>;
		};
	}
	interface Subagent { id: string; name: string; description: string; model?: string; is_builtin: boolean; }

	interface Props {
		profiles: Profile[];
		groups: GroupState;
		allSubagents: Subagent[];
		builtinAgentIds: string[];
		importing?: boolean;
		onEditProfile: (profile: Profile) => void;
		onNewProfile: () => void;
		onGenerate: () => void;
		onImport: () => void;
		onExportProfile: (profileId: string) => void;
		onDeleteProfile: (profileId: string) => void;
		onRevertProfile: (profileId: string) => void;
		getModelDisplay: (model?: string) => string;
		getProfileToolCount: (profile: Profile) => number;
	}

	let {
		profiles,
		groups,
		allSubagents,
		builtinAgentIds,
		importing = false,
		onEditProfile,
		onNewProfile,
		onGenerate,
		onImport,
		onExportProfile,
		onDeleteProfile,
		onRevertProfile,
		getModelDisplay,
		getProfileToolCount
	}: Props = $props();

	const PERM_LABELS: Record<string, string> = {
		default: 'Default',
		acceptEdits: 'Accept Edits',
		plan: 'Plan',
		bypassPermissions: 'Bypass'
	};

	let searchQuery = $state('');
	let deletingId = $state<string | null>(null);
	let deleteLoading = $state(false);
	// Touch/narrow only: which card has its collapsed ⋮ action tray expanded.
	// One open at a time; desktop keeps the always-inline hover-revealed rail.
	let actionsOpenId = $state<string | null>(null);

	// RowActionMenu already stops propagation before calling this, so the card's
	// own click (open the editor) never also fires.
	function toggleRowActions(_e: MouseEvent | KeyboardEvent, id: string) {
		actionsOpenId = actionsOpenId === id ? null : id;
	}
	// The ⋮ toggle and tray buttons all stop propagation, so any click that
	// reaches the window is outside an open tray — collapse it and disarm any
	// pending delete confirmation.
	function onWindowClick() {
		if (actionsOpenId !== null) actionsOpenId = null;
		deletingId = null;
	}
	function onWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			actionsOpenId = null;
			deletingId = null;
		}
	}

	const filtered = $derived(
		searchQuery.trim()
			? profiles.filter(
					(p) =>
						p.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
						(p.description && p.description.toLowerCase().includes(searchQuery.toLowerCase())) ||
						p.id.toLowerCase().includes(searchQuery.toLowerCase())
				)
			: profiles
	);

	function permLabel(mode?: string): string {
		return PERM_LABELS[mode || 'default'] || 'Default';
	}
	function agentCount(p: Profile): number {
		const enabled = p.config?.enabled_agents?.filter((id: string) => allSubagents.some((a) => a.id === id))?.length || 0;
		return Math.max(enabled, builtinAgentIds.length);
	}
	async function confirmDelete() {
		if (!deletingId) return;
		deleteLoading = true;
		try {
			await onDeleteProfile(deletingId);
			deletingId = null;
		} catch (e) {
			console.error('Failed to delete profile:', e);
		} finally {
			deleteLoading = false;
		}
	}
</script>

<svelte:window onclick={onWindowClick} onkeydown={onWindowKeydown} />

<div class="bpg">
	<div class="bpg-toolbar">
		<div class="bpg-search">
			<Search size={14} strokeWidth={1.7} />
			<input type="text" placeholder="Search agents…" autocomplete="off" bind:value={searchQuery} />
		</div>
		<div class="bpg-tools">
			<button class="bpg-btn" onclick={onImport} disabled={importing}>
				<Upload size={14} strokeWidth={1.7} /><span>Import</span>
			</button>
			<button class="bpg-btn" onclick={onGenerate}>
				<Sparkles size={14} strokeWidth={1.7} /><span>Generate</span>
			</button>
		</div>
	</div>

	<div class="bpg-grid">
		{#each filtered as profile (profile.id)}
			{#if deletingId === profile.id}
				<div class="bpg-card bpg-card--deleting">
					<p class="bpg-del-text">Delete <strong>{profile.name}</strong>? This can't be undone.</p>
					<div class="bpg-del-actions">
						<button class="bpg-btn" onclick={(e) => { e.stopPropagation(); deletingId = null; }} disabled={deleteLoading}>Cancel</button>
						<button class="bpg-btn bpg-btn--danger" onclick={(e) => { e.stopPropagation(); confirmDelete(); }} disabled={deleteLoading}>Delete</button>
					</div>
				</div>
			{:else}
				<div
					class="bpg-card"
					role="button"
					tabindex="0"
					onclick={() => onEditProfile(profile)}
					onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onEditProfile(profile); } }}
				>
					<div class="bpg-card-top">
						<span class="bpg-ico"><Users size={16} strokeWidth={1.7} /></span>
						<h3 class="bpg-name">{profile.name}</h3>
					</div>

					<div class="bpg-tags">
						<span class="bpg-pill bpg-pill--accent">{getModelDisplay(profile.config?.model)}</span>
						<span class="bpg-pill">{permLabel(profile.config?.permission_mode)}</span>
						{#if groups?.profiles?.assignments?.[profile.id]}
							<span class="bpg-pill">{groups.profiles.assignments[profile.id]}</span>
						{/if}
					</div>

					{#if profile.description}<p class="bpg-desc">{profile.description}</p>{/if}

					<div class="bpg-meta">
						<span>{getProfileToolCount(profile)} tools</span>
						<span class="bpg-sep">·</span>
						<span>{agentCount(profile)} agents</span>
						<span class="bpg-sep">·</span>
						<span>{profile.session_count || 0} sessions</span>
					</div>

					<div class="bpg-actions" role="toolbar" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
						<RowActionMenu
							open={actionsOpenId === profile.id}
							onToggle={(e) => toggleRowActions(e, profile.id)}
							label="Agent actions"
						>
							{#if profile.is_builtin && profile.is_modified}
								<button class="bpg-act" title="Revert to defaults" onclick={() => { actionsOpenId = null; onRevertProfile(profile.id); }}>
									<RotateCcw size={15} strokeWidth={1.7} />
								</button>
							{/if}
							<button class="bpg-act" title="Export" onclick={() => { actionsOpenId = null; onExportProfile(profile.id); }}>
								<Download size={15} strokeWidth={1.7} />
							</button>
							{#if !profile.is_builtin}
								<button class="bpg-act bpg-act--danger" title="Delete" onclick={() => { actionsOpenId = null; deletingId = profile.id; }}>
									<Trash2 size={15} strokeWidth={1.7} />
								</button>
							{/if}
						</RowActionMenu>
					</div>
				</div>
			{/if}
		{/each}

		<button class="bpg-new" onclick={onNewProfile}>
			<span class="bpg-new-plus"><Plus size={20} strokeWidth={2.2} /></span>
			<span class="bpg-new-label">New agent</span>
			<span class="bpg-new-sub">or generate with AI</span>
		</button>
	</div>

	{#if filtered.length === 0 && searchQuery.trim()}
		<p class="bpg-empty">No agents match “{searchQuery}”.</p>
	{/if}
</div>

<style>
	.bpg { display: flex; flex-direction: column; gap: 16px; }

	/* Toolbar */
	.bpg-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
	.bpg-search { position: relative; display: flex; align-items: center; flex: 1; max-width: 320px; }
	.bpg-search :global(svg) { position: absolute; left: 11px; color: var(--muted-foreground); pointer-events: none; }
	.bpg-search input {
		width: 100%;
		height: 34px;
		padding: 0 12px 0 32px;
		background: var(--input-bg);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		color: var(--foreground);
		font-size: 0.8125rem;
		outline: none;
		transition: border-color 120ms ease, box-shadow 120ms ease;
	}
	.bpg-search input::placeholder { color: var(--muted-foreground); }
	.bpg-search input:focus { border-color: var(--ring); box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 15%, transparent); }
	.bpg-tools { display: flex; align-items: center; gap: 8px; }

	.bpg-btn {
		display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 13px;
		background: var(--card); color: var(--foreground);
		border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) - 2px);
		font-size: 0.8125rem; font-weight: 500; cursor: pointer; white-space: nowrap;
		transition: background-color 120ms ease, border-color 120ms ease;
	}
	.bpg-btn:hover { background: var(--hover-overlay); }
	.bpg-btn:disabled { opacity: 0.5; cursor: not-allowed; }
	.bpg-btn--danger { color: var(--destructive); border-color: color-mix(in oklch, var(--destructive) 35%, var(--border)); }
	.bpg-btn--danger:hover { background: color-mix(in oklch, var(--destructive) 12%, transparent); }

	/* Grid */
	.bpg-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr)); gap: 14px; }

	.bpg-card {
		position: relative;
		display: flex; flex-direction: column;
		min-width: 0;
		padding: 16px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		text-align: left;
		cursor: pointer;
		transition: border-color 140ms ease, box-shadow 140ms ease, transform 140ms ease;
	}
	.bpg-card:hover {
		border-color: color-mix(in oklch, var(--primary) 40%, var(--border));
		box-shadow: var(--shadow-m), 0 0 0 1px color-mix(in oklch, var(--primary) 12%, transparent);
		transform: translateY(-1px);
	}
	.bpg-card:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.bpg-card-top { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
	.bpg-ico {
		display: grid; place-items: center; width: 32px; height: 32px; flex-shrink: 0;
		border-radius: calc(var(--radius, 0.75rem) - 4px);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
	}
	.bpg-name {
		flex: 1; min-width: 0; margin: 0;
		font-family: var(--font-display, Georgia, serif);
		font-size: 0.95rem; font-weight: 700; letter-spacing: -0.01em; color: var(--foreground);
		overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
	}

	.bpg-tags { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 10px; }
	.bpg-pill {
		font-size: 0.6875rem; font-weight: 500;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted) 45%, transparent);
		padding: 2px 8px; border-radius: 6px;
	}
	.bpg-pill--accent { color: var(--primary); background: color-mix(in oklch, var(--primary) 14%, transparent); font-weight: 600; }

	.bpg-desc {
		margin: 0 0 12px; font-size: 0.78rem; line-height: 1.45; color: var(--muted-foreground);
		display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
	}

	.bpg-meta {
		display: flex; align-items: center; gap: 6px; margin-top: auto;
		font-size: 0.6875rem; color: var(--muted-foreground);
	}
	.bpg-meta .bpg-sep { opacity: 0.4; }

	/* Hover actions — a fixed top-right anchor for the RowActionMenu. Reveal of the
	   card's action buttons is signalled to RowActionMenu through the inherited
	   --ram-reveal custom property (0 hidden / 1 shown); the component owns the
	   button opacity + the touch/narrow ⋮ collapse + slide-out. */
	.bpg-actions {
		position: absolute; top: 10px; right: 10px;
		display: flex; gap: 2px;
	}
	.bpg-card { --ram-reveal: 0; }
	.bpg-card:hover, .bpg-card:focus-within { --ram-reveal: 1; }
	.bpg-act {
		display: grid; place-items: center; width: 28px; height: 28px;
		background: transparent; border: 1px solid transparent; border-radius: 7px;
		color: var(--muted-foreground); cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.bpg-act:hover { color: var(--foreground); background: var(--hover-overlay); }
	.bpg-act--danger:hover { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 12%, transparent); }

	/* Inline delete confirm */
	.bpg-card--deleting {
		justify-content: center; gap: 12px; cursor: default;
		border-color: color-mix(in oklch, var(--destructive) 40%, var(--border));
		background: color-mix(in oklch, var(--destructive) 7%, var(--card));
	}
	.bpg-del-text { margin: 0; font-size: 0.8rem; color: var(--foreground); }
	.bpg-del-actions { display: flex; gap: 8px; }

	/* New card */
	.bpg-new {
		display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px;
		min-height: 150px; padding: 16px;
		background: transparent;
		border: 1.5px dashed color-mix(in oklch, var(--muted-foreground) 45%, transparent);
		border-radius: var(--radius, 0.75rem);
		color: var(--muted-foreground); cursor: pointer;
		transition: border-color 140ms ease, color 140ms ease, background-color 140ms ease, transform 140ms ease;
	}
	.bpg-new:hover {
		border-color: var(--primary); color: var(--foreground);
		background: var(--card); transform: translateY(-1px);
	}
	.bpg-new-plus {
		display: grid; place-items: center; width: 40px; height: 40px; border-radius: 50%;
		background: var(--primary); color: var(--primary-foreground);
	}
	.bpg-new-label { font-size: 0.85rem; font-weight: 600; }
	.bpg-new-sub { font-size: 0.7rem; opacity: 0.75; }

	.bpg-empty { font-size: 0.8rem; color: var(--muted-foreground); text-align: center; padding: 8px 0; }

	@media (max-width: 639px) {
		.bpg-grid { grid-template-columns: 1fr; }
		.bpg-search { max-width: none; }
	}
	@media (prefers-reduced-motion: reduce) {
		.bpg-card, .bpg-new { transition: none; }
	}
</style>
