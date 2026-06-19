<script lang="ts">
	/**
	 * SessionsBoard - Mission-control overview of all sessions
	 *
	 * The Sessions section's index. A full-canvas grid of session cards grouped
	 * by project; clicking a card drills into the focused chat. Replaces the
	 * sidebar as the session navigator. Built on the app's theme tokens.
	 */
	import {
		Plus,
		Search,
		X,
		Star,
		FolderClosed,
		MessageSquare,
		ChevronDown,
		ChevronRight
	} from 'lucide-svelte';
	import BrushCleaning from 'lucide-svelte/icons/brush-cleaning';
	import { appData, sessions, projects } from '$lib/stores/app-data.svelte';
	import { activeSession, streamingSessions, backgroundPermissions } from '$lib/stores/active-session';
	import { toggleSessionFavorite } from '$lib/api/client';
	import type { Session } from '$lib/api/client';
	import type { Project } from '$lib/stores/types';
	import { toast } from '$lib/stores/toast';

	interface Props {
		onOpenSession: (sessionId: string) => void;
		onNewSession: () => void;
		onNewSessionForProject?: (projectId: string) => void;
	}

	let { onOpenSession, onNewSession, onNewSessionForProject }: Props = $props();

	let searchQuery = $state('');
	let collapsedProjects = $state<Set<string>>(new Set());
	let deletingSessionId = $state<string | null>(null);
	let deleteLoading = $state(false);

	const activeId = $derived($activeSession.sessionId);

	function isStreaming(sessionId: string): boolean {
		if ($activeSession.sessionId === sessionId) return $activeSession.isStreaming;
		return $streamingSessions.has(sessionId);
	}
	function needsAttention(sessionId: string): boolean {
		return $backgroundPermissions.has(sessionId);
	}

	function sortWithFavorites(list: Session[]): Session[] {
		return [...list].sort((a, b) => {
			if (a.is_favorite && !b.is_favorite) return -1;
			if (!a.is_favorite && b.is_favorite) return 1;
			return 0;
		});
	}

	const filteredSessions = $derived.by(() => {
		if (!searchQuery.trim()) return sortWithFavorites($sessions);
		const q = searchQuery.toLowerCase();
		return sortWithFavorites(
			$sessions.filter(
				(s) => s.title?.toLowerCase().includes(q) || s.id.toLowerCase().includes(q)
			)
		);
	});

	interface ProjectGroup {
		key: string;
		project: Project | null;
		sessions: Session[];
	}

	const groupedSessions = $derived.by((): ProjectGroup[] => {
		const projectMap = new Map<string, Project>();
		for (const p of $projects) projectMap.set(p.id, p);

		const groups = new Map<string, { project: Project | null; sessions: Session[] }>();
		for (const session of filteredSessions) {
			const key = session.project_id || '__ungrouped__';
			if (!groups.has(key)) {
				groups.set(key, {
					project: session.project_id ? projectMap.get(session.project_id) || null : null,
					sessions: []
				});
			}
			groups.get(key)!.sessions.push(session);
		}

		const result: ProjectGroup[] = [];
		const ungrouped = groups.get('__ungrouped__');
		const projectGroups = [...groups.entries()]
			.filter(([key]) => key !== '__ungrouped__')
			.sort(([, a], [, b]) => (a.project?.name || '').localeCompare(b.project?.name || ''));
		for (const [key, group] of projectGroups) {
			result.push({ key, project: group.project, sessions: group.sessions });
		}
		if (ungrouped && ungrouped.sessions.length > 0) {
			result.push({ key: '__ungrouped__', project: null, sessions: ungrouped.sessions });
		}
		return result;
	});

	const totalCount = $derived($sessions.length);

	function isProjectExpanded(key: string): boolean {
		return !collapsedProjects.has(key);
	}
	function toggleProject(key: string) {
		const next = new Set(collapsedProjects);
		if (next.has(key)) next.delete(key); else next.add(key);
		collapsedProjects = next;
	}

	async function handleToggleFavorite(e: MouseEvent | KeyboardEvent, session: Session) {
		e.stopPropagation();
		try {
			await toggleSessionFavorite(session.id);
			await appData.loadSessions();
		} catch {
			toast.push('Failed to update favorite', { tone: 'error' });
		}
	}

	async function handleDelete(e: MouseEvent | KeyboardEvent, sessionId: string) {
		e.stopPropagation();
		if (deletingSessionId === sessionId) {
			deleteLoading = true;
			try {
				if (activeId === sessionId) onNewSession();
				await appData.deleteSession(sessionId);
			} catch {
				toast.push('Failed to delete session', { tone: 'error' });
			} finally {
				deleteLoading = false;
				deletingSessionId = null;
			}
		} else {
			deletingSessionId = sessionId;
			setTimeout(() => { if (deletingSessionId === sessionId) deletingSessionId = null; }, 3000);
		}
	}

	function cancelDelete(e: MouseEvent | KeyboardEvent) {
		e.stopPropagation();
		deletingSessionId = null;
	}

	function formatRelativeTime(dateStr: string): string {
		const date = new Date(dateStr);
		const diffMs = Date.now() - date.getTime();
		const diffMin = Math.floor(diffMs / 60000);
		const diffHr = Math.floor(diffMs / 3600000);
		const diffDay = Math.floor(diffMs / 86400000);
		if (diffMin < 1) return 'now';
		if (diffMin < 60) return `${diffMin}m`;
		if (diffHr < 24) return `${diffHr}h`;
		if (diffDay < 30) return `${diffDay}d`;
		return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}
	function formatCost(cost: number): string {
		if (cost < 0.01) return '<$0.01';
		return `$${cost.toFixed(2)}`;
	}
	function formatDuration(createdAt: string, updatedAt: string): string {
		const diffMs = new Date(updatedAt).getTime() - new Date(createdAt).getTime();
		if (diffMs < 60000) return '<1m';
		const mins = Math.floor(diffMs / 60000);
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
		<div class="board-title-wrap">
			<h1 class="board-title">Sessions</h1>
			<span class="board-count">{totalCount}</span>
		</div>
		<div class="board-header-actions">
			<div class="board-search">
				<Search size={15} strokeWidth={1.6} />
				<input
					type="text"
					autocomplete="off"
					placeholder="Search sessions..."
					bind:value={searchQuery}
				/>
				{#if searchQuery}
					<button class="board-search-clear" onclick={() => (searchQuery = '')} aria-label="Clear search">
						<X size={13} strokeWidth={2} />
					</button>
				{/if}
			</div>
			<button class="board-new" onclick={onNewSession}>
				<Plus size={16} strokeWidth={2} /><span>New session</span>
			</button>
		</div>
	</header>

	<!-- Body -->
	<div class="board-body">
		{#if groupedSessions.length === 0}
			<div class="board-empty">
				<MessageSquare size={40} strokeWidth={1.25} />
				<p class="board-empty-title">{searchQuery ? 'No sessions match your search' : 'No sessions yet'}</p>
				{#if !searchQuery}
					<button class="board-new" onclick={onNewSession}>
						<Plus size={16} strokeWidth={2} /><span>Start your first session</span>
					</button>
				{/if}
			</div>
		{:else}
			{#each groupedSessions as group (group.key)}
				<section class="board-group">
					<div class="board-group-header">
						<button class="board-group-toggle" onclick={() => toggleProject(group.key)}>
							{#if isProjectExpanded(group.key)}
								<ChevronDown size={14} strokeWidth={1.6} />
							{:else}
								<ChevronRight size={14} strokeWidth={1.6} />
							{/if}
							<FolderClosed size={14} strokeWidth={1.6} />
							<span class="board-group-name">{group.project?.name || 'Ungrouped'}</span>
							<span class="board-group-count">{group.sessions.length}</span>
						</button>
						{#if group.project && onNewSessionForProject}
							<button
								class="board-group-new"
								title="New session in this project"
								onclick={() => onNewSessionForProject?.(group.project!.id)}
							>
								<Plus size={14} strokeWidth={1.75} />
							</button>
						{/if}
					</div>

					{#if isProjectExpanded(group.key)}
						<div class="board-grid">
							{#each group.sessions as session (session.id)}
								<button
									class="board-card"
									class:active={activeId === session.id}
									class:deleting={deletingSessionId === session.id}
									onclick={() => onOpenSession(session.id)}
								>
									<div class="card-top">
										<span class="card-status">
											{#if isStreaming(session.id)}
												<span class="dot dot-streaming" title="Running"></span>
											{:else if needsAttention(session.id)}
												<span class="dot dot-attention" title="Needs attention"></span>
											{:else}
												<span class="dot dot-idle"></span>
											{/if}
										</span>
										<span class="card-title">{session.title || 'Untitled'}</span>
										<span
											class="card-fav"
											class:is-fav={session.is_favorite}
											role="button"
											tabindex="0"
											title={session.is_favorite ? 'Unfavorite' : 'Favorite'}
											onclick={(e) => handleToggleFavorite(e, session)}
											onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') handleToggleFavorite(e, session); }}
										>
											<Star size={13} strokeWidth={1.6} fill={session.is_favorite ? 'currentColor' : 'none'} />
										</span>
									</div>

									{#if session.total_cost_usd > 0}
										<div class="card-meta">
											<span>{formatCost(session.total_cost_usd)}</span>
											<span class="sep">·</span>
											<span>{formatDuration(session.created_at, session.updated_at || session.created_at)}</span>
											{#if session.turn_count > 0}
												<span class="sep">·</span>
												<span>{session.turn_count} {session.turn_count === 1 ? 'turn' : 'turns'}</span>
											{/if}
										</div>
									{:else}
										<div class="card-meta card-meta-empty">No activity yet</div>
									{/if}

									<div class="card-foot">
										<span class="card-time">{formatRelativeTime(session.updated_at || session.created_at)}</span>
										{#if deletingSessionId === session.id}
											<span class="card-confirm">
												<span
													class="card-action danger"
													class:disabled={deleteLoading}
													role="button"
													tabindex="0"
													title="Confirm clear"
													onclick={(e) => { if (!deleteLoading) handleDelete(e, session.id); }}
													onkeydown={(e) => { if ((e.key === 'Enter' || e.key === ' ') && !deleteLoading) handleDelete(e, session.id); }}
												>
													<BrushCleaning size={13} strokeWidth={2} />
												</span>
												<span
													class="card-action"
													role="button"
													tabindex="0"
													title="Cancel"
													onclick={(e) => cancelDelete(e)}
													onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') cancelDelete(e); }}
												>
													<X size={13} strokeWidth={2} />
												</span>
											</span>
										{:else}
											<span
												class="card-action card-clear"
												role="button"
												tabindex="0"
												title="Clear session"
												onclick={(e) => handleDelete(e, session.id)}
												onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') handleDelete(e, session.id); }}
											>
												<BrushCleaning size={13} strokeWidth={1.6} />
											</span>
										{/if}
									</div>
								</button>
							{/each}
						</div>
					{/if}
				</section>
			{/each}
		{/if}
	</div>
</div>

<style>
	.board {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		background: var(--background);
		overflow: hidden;
	}

	/* Header */
	.board-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		flex-wrap: wrap;
		padding: 16px 24px;
		border-bottom: 1px solid var(--border-subtle, var(--border));
		flex-shrink: 0;
	}
	.board-title-wrap { display: flex; align-items: baseline; gap: 10px; }
	.board-title {
		font-family: var(--font-display, Georgia, serif);
		font-size: 1.35rem;
		font-weight: 700;
		letter-spacing: -0.01em;
		color: var(--foreground);
	}
	.board-count {
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		padding: 2px 8px;
		border-radius: 999px;
	}
	.board-header-actions { display: flex; align-items: center; gap: 10px; }

	.board-search {
		position: relative;
		display: flex;
		align-items: center;
	}
	.board-search :global(svg) {
		position: absolute;
		left: 10px;
		color: var(--muted-foreground);
		pointer-events: none;
	}
	.board-search input {
		width: 240px;
		max-width: 46vw;
		height: 34px;
		padding: 0 28px 0 32px;
		background: var(--input-bg);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		color: var(--foreground);
		font-size: 0.8125rem;
		outline: none;
		transition: border-color 120ms ease, box-shadow 120ms ease;
	}
	.board-search input::placeholder { color: var(--muted-foreground); }
	.board-search input:focus {
		border-color: var(--ring);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 15%, transparent);
	}
	.board-search-clear {
		position: absolute;
		right: 6px;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 20px;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: var(--muted-foreground);
		cursor: pointer;
	}
	.board-search-clear:hover { color: var(--foreground); background: var(--hover-overlay); }

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

	/* Body */
	.board-body {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 16px 24px 32px;
		display: flex;
		flex-direction: column;
		gap: 18px;
	}

	.board-group { display: flex; flex-direction: column; gap: 10px; }
	.board-group-header {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.board-group-toggle {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1;
		min-width: 0;
		padding: 4px 2px;
		background: transparent;
		border: none;
		color: var(--muted-foreground);
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		cursor: pointer;
		text-align: left;
		transition: color 120ms ease;
	}
	.board-group-toggle:hover { color: var(--foreground); }
	.board-group-name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		text-transform: none;
		letter-spacing: 0;
		font-size: 0.8125rem;
	}
	.board-group-count {
		font-size: 0.625rem;
		font-weight: 600;
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		padding: 1px 6px;
		border-radius: 999px;
	}
	.board-group-new {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 26px;
		height: 26px;
		background: transparent;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) - 4px);
		color: var(--muted-foreground);
		cursor: pointer;
		flex-shrink: 0;
		transition: all 120ms ease;
	}
	.board-group-new:hover { color: var(--primary); border-color: var(--primary); background: color-mix(in oklch, var(--primary) 10%, transparent); }

	/* Card grid */
	.board-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
		gap: 12px;
	}

	.board-card {
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 14px;
		min-height: 104px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		text-align: left;
		cursor: pointer;
		transition: border-color 140ms ease, box-shadow 140ms ease, transform 140ms ease;
	}
	.board-card:hover {
		border-color: color-mix(in oklch, var(--primary) 40%, var(--border));
		box-shadow: var(--shadow-m), 0 0 0 1px color-mix(in oklch, var(--primary) 12%, transparent);
		transform: translateY(-1px);
	}
	.board-card:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.board-card.active {
		border-color: var(--primary);
		box-shadow: 0 0 0 1px var(--primary), var(--shadow-m);
	}
	.board-card.deleting {
		border-color: color-mix(in oklch, var(--destructive) 45%, transparent);
		background: color-mix(in oklch, var(--destructive) 7%, var(--card));
	}

	.card-top { display: flex; align-items: flex-start; gap: 8px; }
	.card-status { display: flex; align-items: center; padding-top: 4px; flex-shrink: 0; }
	.dot { width: 7px; height: 7px; border-radius: 50%; display: block; }
	.dot-streaming { background: var(--primary); animation: pulse 2s ease-in-out infinite; }
	.dot-attention { background: var(--warning); animation: pulse 1.5s ease-in-out infinite; }
	.dot-idle { background: color-mix(in oklch, var(--muted-foreground) 50%, transparent); }
	@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

	.card-title {
		flex: 1;
		min-width: 0;
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--foreground);
		line-height: 1.35;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
	.card-fav {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 22px;
		height: 22px;
		border-radius: 6px;
		color: var(--muted-foreground);
		opacity: 0;
		cursor: pointer;
		transition: opacity 120ms ease, color 120ms ease, background-color 120ms ease;
	}
	.card-fav.is-fav { opacity: 1; color: var(--warning); }
	.board-card:hover .card-fav { opacity: 1; }
	.card-fav:hover { color: var(--warning); background: var(--hover-overlay); }

	.card-meta {
		display: flex;
		align-items: center;
		gap: 5px;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		flex-wrap: wrap;
	}
	.card-meta .sep { opacity: 0.4; }
	.card-meta-empty { font-style: italic; opacity: 0.7; }

	.card-foot {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-top: auto;
	}
	.card-time { font-size: 0.6875rem; color: var(--muted-foreground); opacity: 0.7; }

	.card-confirm { display: flex; gap: 4px; }
	.card-action {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 24px;
		height: 24px;
		border-radius: 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.card-clear { opacity: 0; }
	.board-card:hover .card-clear { opacity: 1; }
	.card-action:hover { color: var(--foreground); background: var(--hover-overlay); }
	.card-action.danger { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 12%, transparent); }
	.card-action.danger:hover { background: color-mix(in oklch, var(--destructive) 20%, transparent); }
	.card-action.disabled { opacity: 0.5; cursor: not-allowed; }

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

	/* Touch / mobile: always reveal hover actions */
	@media (hover: none), (max-width: 639px) {
		.card-fav, .card-clear { opacity: 0.7; }
	}
	@media (max-width: 639px) {
		.board-header { padding: 12px 16px; }
		.board-body { padding: 12px 16px 28px; }
		.board-grid { grid-template-columns: 1fr; }
		.board-search input { width: 100%; }
		.board-header-actions { width: 100%; }
		.board-search { flex: 1; }
	}

	@media (prefers-reduced-motion: reduce) {
		.board-card { transition: none; }
		.dot-streaming, .dot-attention { animation: none; }
	}
</style>
