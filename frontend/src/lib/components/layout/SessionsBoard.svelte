<script lang="ts">
	/**
	 * SessionsBoard - Mission-control navigator (scales to many projects/sessions)
	 *
	 * Two levels:
	 *  - Overview: a "Recent" strip + a grid of project cards (one per project,
	 *    with session count / running / last-active). Avoids rendering thousands
	 *    of session cards at once.
	 *  - Project: a dense, searchable session list (rows) for the chosen project.
	 * A global search box flips the overview into a flat results list.
	 *
	 * Built on the app's theme tokens.
	 */
	import { Plus, X, Star, FolderClosed, MessageSquare, ChevronRight, GitBranch } from 'lucide-svelte';
	import BrushCleaning from 'lucide-svelte/icons/brush-cleaning';
	import { appData, sessions, projects } from '$lib/stores/app-data.svelte';
	import { activeSession, streamingSessions, backgroundPermissions } from '$lib/stores/active-session';
	import { toggleSessionFavorite } from '$lib/api/client';
	import type { Session } from '$lib/api/client';
	import { toast } from '$lib/stores/toast';

	interface Props {
		onOpenSession: (sessionId: string) => void;
		onNewSession: () => void;
		onNewSessionForProject?: (projectId: string) => void;
	}

	// onNewSessionForProject is part of the component contract but the board no
	// longer renders its own "New session" button (the top bar provides it), so
	// it is intentionally left undestructured.
	let { onOpenSession, onNewSession }: Props = $props();

	const UNGROUPED = '__ungrouped__';
	const RECENT_LIMIT = 6;

	let selectedKey = $state<string | null>(null);
	let deletingSessionId = $state<string | null>(null);
	let deleteLoading = $state(false);

	const activeId = $derived($activeSession.sessionId);
	const projectsById = $derived(new Map($projects.map((p) => [p.id, p])));

	function isStreaming(id: string): boolean {
		if ($activeSession.sessionId === id) return $activeSession.isStreaming;
		return $streamingSessions.has(id);
	}
	function needsAttention(id: string): boolean {
		return $backgroundPermissions.has(id);
	}
	function tsOf(s: Session): number {
		return new Date(s.updated_at || s.created_at).getTime();
	}
	function sortSessions(list: Session[]): Session[] {
		return [...list].sort((a, b) => {
			if (!!a.is_favorite !== !!b.is_favorite) return a.is_favorite ? -1 : 1;
			return tsOf(b) - tsOf(a);
		});
	}
	function projectName(s: Session): string {
		return s.project_id ? projectsById.get(s.project_id)?.name || 'Ungrouped' : 'Ungrouped';
	}

	const totalCount = $derived($sessions.length);

	interface Group {
		key: string;
		name: string;
		projectId: string | null;
		sessions: Session[];
		lastActivity: number;
		running: number;
		latest: Session | null;
	}

	const projectGroups = $derived.by((): Group[] => {
		const groups = new Map<string, Session[]>();
		for (const s of $sessions) {
			const key = s.project_id || UNGROUPED;
			if (!groups.has(key)) groups.set(key, []);
			groups.get(key)!.push(s);
		}
		const arr: Group[] = [...groups.entries()].map(([key, list]) => {
			const sorted = sortSessions(list);
			return {
				key,
				name: key === UNGROUPED ? 'Ungrouped' : projectsById.get(key)?.name || 'Ungrouped',
				projectId: key === UNGROUPED ? null : key,
				sessions: list,
				lastActivity: list.reduce((m, s) => Math.max(m, tsOf(s)), 0),
				running: list.filter((s) => isStreaming(s.id)).length,
				latest: sorted[0] ?? null
			};
		});
		arr.sort((a, b) => b.lastActivity - a.lastActivity);
		return arr;
	});

	// Pinned (favorited) sessions get their own section so they always stay
	// visible at the top instead of competing with everything else for the
	// limited Recent slots. Recent therefore excludes favorites.
	const pinned = $derived($sessions.filter((s) => s.is_favorite).sort((a, b) => tsOf(b) - tsOf(a)));
	const recent = $derived(
		$sessions.filter((s) => !s.is_favorite).sort((a, b) => tsOf(b) - tsOf(a)).slice(0, RECENT_LIMIT)
	);

	const selectedGroup = $derived(selectedKey ? projectGroups.find((g) => g.key === selectedKey) ?? null : null);

	type Mode = 'overview' | 'project';
	const mode = $derived<Mode>(selectedGroup ? 'project' : 'overview');

	const displayList = $derived.by((): Session[] =>
		selectedGroup ? sortSessions(selectedGroup.sessions) : []
	);

	function openProject(key: string) {
		selectedKey = key;
	}
	function backToOverview() {
		selectedKey = null;
	}

	async function handleToggleFavorite(e: MouseEvent | KeyboardEvent, s: Session) {
		e.stopPropagation();
		try {
			await toggleSessionFavorite(s.id);
			await appData.loadSessions();
		} catch {
			toast.push('Failed to update favorite', { tone: 'error' });
		}
	}
	async function handleDelete(e: MouseEvent | KeyboardEvent, id: string) {
		e.stopPropagation();
		if (deletingSessionId === id) {
			deleteLoading = true;
			try {
				if (activeId === id) onNewSession();
				await appData.deleteSession(id);
			} catch {
				toast.push('Failed to delete session', { tone: 'error' });
			} finally {
				deleteLoading = false;
				deletingSessionId = null;
			}
		} else {
			deletingSessionId = id;
			setTimeout(() => { if (deletingSessionId === id) deletingSessionId = null; }, 3000);
		}
	}
	function cancelDelete(e: MouseEvent | KeyboardEvent) {
		e.stopPropagation();
		deletingSessionId = null;
	}
	// Open a session from its row — but never while that row is showing its
	// delete confirmation (or the delete is in flight). The confirm button is
	// natively `disabled` during the request, and the browser routes a click on
	// a disabled button to its parent (the row), which would otherwise open the
	// just-deleted session as an empty chat.
	function openSession(id: string) {
		if (deletingSessionId === id) return;
		onOpenSession(id);
	}

	function relTime(input: string | number): string {
		const ms = typeof input === 'number' ? input : new Date(input).getTime();
		if (!ms) return '';
		const diff = Date.now() - ms;
		const min = Math.floor(diff / 60000);
		const hr = Math.floor(diff / 3600000);
		const day = Math.floor(diff / 86400000);
		if (min < 1) return 'now';
		if (min < 60) return `${min}m`;
		if (hr < 24) return `${hr}h`;
		if (day < 30) return `${day}d`;
		return new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}
	function fmtCost(c: number): string {
		return c < 0.01 ? '<$0.01' : `$${c.toFixed(2)}`;
	}
	function fmtDuration(s: Session): string {
		const diff = new Date(s.updated_at || s.created_at).getTime() - new Date(s.created_at).getTime();
		if (diff < 60000) return '<1m';
		const mins = Math.floor(diff / 60000);
		if (mins < 60) return `${mins}m`;
		const hrs = Math.floor(mins / 60);
		const rem = mins % 60;
		if (hrs < 24) return rem > 0 ? `${hrs}h ${rem}m` : `${hrs}h`;
		return `${Math.floor(hrs / 24)}d`;
	}
</script>

<div class="board card-system">
	<!-- Header -->
	<header class="board-header">
		<div class="board-head-left">
			{#if mode === 'project' && selectedGroup}
				<button class="crumb" onclick={backToOverview}>Sessions</button>
				<ChevronRight size={14} strokeWidth={1.6} class="crumb-sep" />
				<span class="crumb-current">{selectedGroup.name}</span>
				<span class="board-count">{selectedGroup.sessions.length}</span>
			{:else}
				<h1 class="board-title">Sessions</h1>
				<span class="board-count">{totalCount}</span>
			{/if}
		</div>
	</header>

	<!-- Body -->
	<div class="board-body">
		<div class="board-wrap">
			{#if mode === 'overview'}
				{#if totalCount === 0}
					<div class="board-empty">
						<MessageSquare size={40} strokeWidth={1.25} />
						<p class="board-empty-title">No sessions yet</p>
						<button class="board-new" onclick={onNewSession}>
							<Plus size={16} strokeWidth={2} /><span>Start your first session</span>
						</button>
					</div>
				{:else}
					{#if pinned.length}
						<section class="board-section">
							<h2 class="section-label">
								<Star size={12} strokeWidth={2} fill="currentColor" class="section-icon" />
								Pinned <span class="section-count">{pinned.length}</span>
							</h2>
							<div class="row-list">
								{#each pinned as s (s.id)}{@render sessionRow(s, true)}{/each}
							</div>
						</section>
					{/if}
					{#if recent.length}
						<section class="board-section">
							<h2 class="section-label">Recent</h2>
							<div class="row-list">
								{#each recent as s (s.id)}{@render sessionRow(s, true)}{/each}
							</div>
						</section>
					{/if}
					<section class="board-section">
						<h2 class="section-label">Projects <span class="section-count">{projectGroups.length}</span></h2>
						<div class="proj-grid">
							{#each projectGroups as g (g.key)}
								<button class="proj-card" onclick={() => openProject(g.key)}>
									<div class="proj-top">
										<span class="proj-icon"><FolderClosed size={16} strokeWidth={1.6} /></span>
										<span class="proj-name">{g.name}</span>
										{#if g.running > 0}
											<span class="proj-running"><span class="dot dot-streaming"></span>{g.running}</span>
										{/if}
									</div>
									<div class="proj-latest">{g.latest?.title || 'Untitled'}</div>
									<div class="proj-stats">
										<span class="proj-count-pill">{g.sessions.length}</span>
										<span>{g.sessions.length === 1 ? 'session' : 'sessions'}</span>
										<span class="sep">·</span>
										<span>{relTime(g.lastActivity)}</span>
										<ChevronRight size={14} strokeWidth={1.6} class="proj-chevron" />
									</div>
								</button>
							{/each}
						</div>
					</section>
				{/if}
			{:else}
				<!-- project: dense session list -->
				{#if displayList.length === 0}
					<div class="board-empty">
						<MessageSquare size={36} strokeWidth={1.25} />
						<p class="board-empty-title">No sessions in this project</p>
					</div>
				{:else}
					<div class="row-list">
						{#each displayList as s (s.id)}{@render sessionRow(s, false)}{/each}
					</div>
				{/if}
			{/if}
		</div>
	</div>
</div>

{#snippet sessionRow(session: Session, showProject: boolean)}
	<div
		class="row"
		class:active={activeId === session.id}
		class:deleting={deletingSessionId === session.id}
		role="button"
		tabindex="0"
		onclick={() => openSession(session.id)}
		onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openSession(session.id); } }}
	>
		<span class="row-status">
			{#if isStreaming(session.id)}
				<span class="dot dot-streaming" title="Running"></span>
			{:else if needsAttention(session.id)}
				<span class="dot dot-attention" title="Needs attention"></span>
			{:else}
				<span class="dot dot-idle"></span>
			{/if}
		</span>
		<div class="row-main">
			<div class="row-line1">
				<span class="row-title">{session.title || 'Untitled'}</span>
				<span class="row-time">{relTime(session.updated_at || session.created_at)}</span>
			</div>
			<div class="row-line2">
				{#if showProject}
					<span class="row-proj">{projectName(session)}</span>
				{/if}
				{#if session.total_cost_usd > 0}
					<span class="row-stat">{fmtCost(session.total_cost_usd)}</span>
					{#if session.turn_count > 0}
						<span class="sep">·</span>
						<span class="row-stat">{session.turn_count} {session.turn_count === 1 ? 'turn' : 'turns'}</span>
					{/if}
					<span class="sep">·</span>
					<span class="row-stat">{fmtDuration(session)}</span>
				{:else}
					<span class="row-stat muted">No activity yet</span>
				{/if}
				{#if session.worktree_branch}
					<span class="row-branch"><GitBranch size={11} strokeWidth={1.6} />{session.worktree_branch}</span>
				{/if}
			</div>
		</div>
		<span class="row-actions">
			<button
				class="row-action fav"
				class:is-fav={session.is_favorite}
				title={session.is_favorite ? 'Unfavorite' : 'Favorite'}
				onclick={(e) => handleToggleFavorite(e, session)}
			>
				<Star size={13} strokeWidth={1.6} fill={session.is_favorite ? 'currentColor' : 'none'} />
			</button>
			{#if deletingSessionId === session.id}
				<button class="row-action danger" disabled={deleteLoading} title="Confirm clear" onclick={(e) => handleDelete(e, session.id)}>
					<BrushCleaning size={13} strokeWidth={2} />
				</button>
				<button class="row-action" title="Cancel" onclick={cancelDelete}>
					<X size={13} strokeWidth={2} />
				</button>
			{:else}
				<button class="row-action clear" title="Clear session" onclick={(e) => handleDelete(e, session.id)}>
					<BrushCleaning size={13} strokeWidth={1.6} />
				</button>
			{/if}
		</span>
	</div>
{/snippet}

<style>
	.board {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		background: var(--background);
		overflow: hidden;
	}

	/* Header — title + count centered */
	.board-header {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 16px;
		padding: 16px 24px;
		border-bottom: 1px solid var(--border-subtle, var(--border));
		flex-shrink: 0;
	}
	.board-head-left { display: flex; align-items: center; justify-content: center; gap: 8px; min-width: 0; }
	.board-title {
		font-family: var(--font-display, Georgia, serif);
		font-size: 1.35rem;
		font-weight: 700;
		letter-spacing: -0.01em;
		color: var(--foreground);
	}
	.crumb {
		background: transparent;
		border: none;
		padding: 0;
		font-family: var(--font-display, Georgia, serif);
		font-size: 1.35rem;
		font-weight: 700;
		letter-spacing: -0.01em;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 120ms ease;
	}
	.crumb:hover { color: var(--foreground); }
	.board-head-left :global(.crumb-sep) { color: var(--muted-foreground); opacity: 0.6; }
	.crumb-current {
		font-family: var(--font-display, Georgia, serif);
		font-size: 1.35rem;
		font-weight: 700;
		letter-spacing: -0.01em;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.board-count {
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		padding: 2px 8px;
		border-radius: 999px;
		flex-shrink: 0;
	}

	.board-new {
		display: inline-flex;
		align-items: center;
		gap: 7px;
		height: 34px;
		padding: 0 14px;
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		font-size: 0.8125rem;
		font-weight: 600;
		cursor: pointer;
		white-space: nowrap;
		transition: filter 120ms ease;
	}
	.board-new:hover { filter: brightness(1.08); }

	/* Body — full-width scroll container; inner content centered at 1240px to
	   match every other Studio-v2 surface (the mockup's `.wrap`). */
	.board-body {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 24px 24px 40px;
		display: flex;
		flex-direction: column;
	}
	.board-wrap {
		width: 100%;
		max-width: 1240px;
		margin: 0 auto;
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 22px;
	}

	.board-section { display: flex; flex-direction: column; gap: 10px; flex-shrink: 0; }
	.section-label {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--muted-foreground);
	}
	.section-count {
		font-size: 0.625rem;
		font-weight: 600;
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		padding: 1px 6px;
		border-radius: 999px;
		letter-spacing: 0;
	}
	.section-label :global(.section-icon) { color: var(--warning); }

	/* Project grid */
	.proj-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(min(100%, 280px), 1fr));
		gap: 12px;
	}
	.proj-card {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 14px 16px;
		min-width: 0;
		max-width: 100%;
		overflow: hidden;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		text-align: left;
		cursor: pointer;
		transition: border-color 140ms ease, box-shadow 140ms ease, transform 140ms ease;
	}
	.proj-card:hover {
		border-color: color-mix(in oklch, var(--primary) 40%, var(--border));
		box-shadow: var(--shadow-m), 0 0 0 1px color-mix(in oklch, var(--primary) 12%, transparent);
		transform: translateY(-1px);
	}
	.proj-card:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.proj-top { display: flex; align-items: center; gap: 8px; min-width: 0; }
	.proj-icon { display: flex; color: var(--muted-foreground); flex-shrink: 0; }
	.proj-name {
		flex: 1;
		min-width: 0;
		font-size: 0.9rem;
		font-weight: 600;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.proj-running {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--primary);
		flex-shrink: 0;
	}
	.proj-latest {
		max-width: 100%;
		font-size: 0.78rem;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.proj-stats {
		display: flex;
		align-items: center;
		gap: 5px;
		min-width: 0;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}
	.proj-stats .sep { opacity: 0.4; }
	.proj-count-pill {
		font-weight: 600;
		color: var(--foreground);
	}
	.proj-card :global(.proj-chevron) { margin-left: auto; color: var(--muted-foreground); opacity: 0.5; }

	/* Dense session rows */
	.row-list {
		display: flex;
		flex-direction: column;
		flex-shrink: 0;
		border: 1px solid var(--border-subtle, var(--border));
		border-radius: var(--radius, 0.75rem);
		overflow: hidden;
		background: var(--card);
	}
	.row {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 9px 14px;
		min-height: 56px;
		cursor: pointer;
		border-bottom: 1px solid var(--border-subtle, var(--border));
		transition: background-color 120ms ease;
	}
	.row:last-child { border-bottom: none; }
	.row:hover { background: var(--hover-overlay); }
	.row:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.row.active { background: var(--selection-bg, color-mix(in oklch, var(--primary) 10%, transparent)); box-shadow: inset 3px 0 0 var(--primary); }
	.row.deleting { background: color-mix(in oklch, var(--destructive) 8%, transparent); box-shadow: inset 3px 0 0 var(--destructive); }

	.row-status { display: flex; align-items: center; flex-shrink: 0; }
	.dot { width: 8px; height: 8px; border-radius: 50%; display: block; }
	.dot-streaming { background: var(--primary); animation: pulse 2s ease-in-out infinite; }
	.dot-attention { background: var(--warning); animation: pulse 1.5s ease-in-out infinite; }
	.dot-idle { background: color-mix(in oklch, var(--muted-foreground) 45%, transparent); }
	@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

	.row-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
	.row-line1 { display: flex; align-items: center; gap: 8px; }
	.row-title {
		flex: 1;
		min-width: 0;
		font-size: 0.85rem;
		font-weight: 500;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.row-time { flex-shrink: 0; font-size: 0.6875rem; color: var(--muted-foreground); opacity: 0.75; }

	.row-line2 {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		min-width: 0;
		overflow: hidden;
	}
	.row-line2 .sep { opacity: 0.4; }
	.row-line2 .muted { font-style: italic; opacity: 0.7; }
	.row-stat { white-space: nowrap; }
	.row-proj {
		flex-shrink: 0;
		max-width: 150px;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted) 45%, transparent);
		padding: 1px 7px;
		border-radius: 999px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.row-branch {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		max-width: 160px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: color-mix(in oklch, var(--primary) 85%, var(--muted-foreground));
	}
	.row-branch :global(svg) { flex-shrink: 0; }

	.row-actions { display: flex; align-items: center; gap: 2px; flex-shrink: 0; }
	.row-action {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 26px;
		height: 26px;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		opacity: 0;
		transition: opacity 120ms ease, color 120ms ease, background-color 120ms ease;
	}
	.row:hover .row-action { opacity: 1; }
	.row-action:hover { color: var(--foreground); background: color-mix(in oklch, var(--muted) 60%, transparent); }
	.row-action.fav.is-fav { opacity: 1; color: var(--warning); }
	.row-action.fav:hover { color: var(--warning); }
	.row-action.danger { opacity: 1; color: var(--destructive); background: color-mix(in oklch, var(--destructive) 12%, transparent); }
	.row-action.danger:hover { background: color-mix(in oklch, var(--destructive) 20%, transparent); }
	.row-action:disabled { opacity: 0.5; cursor: not-allowed; }

	/* Empty */
	.board-empty {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 14px;
		color: var(--muted-foreground);
		text-align: center;
		padding: 48px 16px;
	}
	.board-empty :global(svg) { opacity: 0.5; }
	.board-empty-title { font-size: 0.9rem; }

	/* Touch / mobile */
	@media (hover: none), (max-width: 639px) {
		.row-action { opacity: 0.7; }
	}
	@media (max-width: 639px) {
		.board-header { padding: 12px 16px; }
		.board-body { padding: 12px 16px 28px; }
		.proj-grid { grid-template-columns: 1fr; }
		.row { min-height: 64px; padding: 11px 14px; gap: 11px; }
		.row-title { font-size: 0.9rem; }
		.row-line2 { font-size: 0.72rem; gap: 7px; }
		.row-action { width: 38px; height: 38px; opacity: 0.8; }
	}

	@media (prefers-reduced-motion: reduce) {
		.proj-card { transition: none; }
		.dot-streaming, .dot-attention { animation: none; }
	}
</style>
