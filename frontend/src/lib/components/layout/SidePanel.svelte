<script lang="ts">
	/**
	 * SidePanel - Codex-style collapsible sidebar
	 *
	 * Sections:
	 * 1. Header: New session button
	 * 2. Navigation: Feature page links (Settings, Files, Plugins, etc.)
	 * 3. Recent Sessions: Last few sessions with timestamps
	 * 4. Sessions: Full list grouped by project with search
	 */
	import {
		Plus,
		Settings,
		FolderOpen,
		Puzzle,
		Server,
		StickyNote,
		Webhook,
		BarChart3,
		Search,
		X,
		PanelLeftClose,
		Users,
		ChevronDown,
		ChevronRight,
		FolderClosed,
		Star,
		Trash2,
		Bot,
		FolderKanban
	} from 'lucide-svelte';
	import { layout, activeView, sidebarCollapsed } from '$lib/stores/layout';
	import type { ViewType } from '$lib/stores/layout';
	import { appData, sessions, projects } from '$lib/stores/app-data';
	import { activeSession, streamingSessions, backgroundPermissions } from '$lib/stores/active-session';
	import { toggleSessionFavorite } from '$lib/api/client';
	import type { Session } from '$lib/api/client';
	import type { Project } from '$lib/stores/types';

	interface Props {
		isAdmin?: boolean;
		onNewSession: () => void;
		onOpenSession: (sessionId: string) => void;
		onOpenFeature: (type: ViewType) => void;
	}

	let {
		isAdmin = false,
		onNewSession,
		onOpenSession,
		onOpenFeature
	}: Props = $props();

	// Search state
	let searchQuery = $state('');
	let searchInputRef = $state<HTMLInputElement | null>(null);
	let navExpanded = $state(true);
	let sessionsExpanded = $state(true);

	// Track which project groups are COLLAPSED (empty set = all expanded by default)
	// This avoids needing a $effect to sync with project list, preventing infinite loops
	let collapsedProjects = $state<Set<string>>(new Set());

	// Track project being deleted (for confirmation)
	let deletingProjectId = $state<string | null>(null);
	let deleteLoading = $state(false);

	// Navigation items
	const navItems: { type: ViewType; label: string; icon: typeof Settings; adminOnly?: boolean }[] = [
		{ type: 'file-browser', label: 'Files', icon: FolderOpen },
		{ type: 'plugins', label: 'Plugins', icon: Puzzle },
		{ type: 'mcp', label: 'MCP Servers', icon: Server },
		{ type: 'subagent', label: 'Subagents', icon: Bot },
		{ type: 'notes', label: 'Notes', icon: StickyNote },
		{ type: 'hooks', label: 'Hooks', icon: Webhook },
		{ type: 'stats', label: 'Stats', icon: BarChart3 },
		{ type: 'profile', label: 'Profiles', icon: Users },
		{ type: 'project', label: 'Projects', icon: FolderKanban },
		{ type: 'settings', label: 'Settings', icon: Settings, adminOnly: true }
	];

	const filteredNavItems = $derived(
		navItems.filter((item) => !item.adminOnly || isAdmin)
	);

	// Sort sessions with favorites first, preserving existing order within each group
	function sortWithFavorites(list: Session[]): Session[] {
		return [...list].sort((a, b) => {
			if (a.is_favorite && !b.is_favorite) return -1;
			if (!a.is_favorite && b.is_favorite) return 1;
			return 0;
		});
	}

	// Toggle favorite status on a session
	async function handleToggleFavorite(e: MouseEvent | KeyboardEvent, session: Session) {
		e.stopPropagation();
		try {
			await toggleSessionFavorite(session.id);
			await appData.loadSessions();
		} catch (err) {
			console.error('Failed to toggle favorite:', err);
		}
	}

	// Sessions: recent (top 5) and all — favorites sorted first
	// Filter out sessions with no chat history (turn_count === 0) - these are
	// either empty or were fully rewound and have no useful content to show.
	// Always keep the active session visible (new sessions start at 0 turns
	// until the first query completes).
	const activeId = $derived($activeSession.sessionId);
	const sessionsWithHistory = $derived(
		$sessions.filter(s => s.turn_count > 0 || s.id === activeId)
	);

	const recentSessions = $derived(
		sortWithFavorites(sessionsWithHistory).slice(0, 5)
	);

	const filteredSessions = $derived.by(() => {
		if (!searchQuery.trim()) return sortWithFavorites(sessionsWithHistory);
		const q = searchQuery.toLowerCase();
		return sortWithFavorites(
			sessionsWithHistory.filter(
				(s) =>
					s.title?.toLowerCase().includes(q) ||
					s.id.toLowerCase().includes(q)
			)
		);
	});

	// Group sessions by project
	interface ProjectGroup {
		project: Project | null;
		key: string;
		sessions: Session[];
	}

	const groupedSessions = $derived.by((): ProjectGroup[] => {
		const projectMap = new Map<string, Project>();
		for (const p of $projects) {
			projectMap.set(p.id, p);
		}

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

		// Add project groups sorted alphabetically
		const projectGroups = [...groups.entries()]
			.filter(([key]) => key !== '__ungrouped__')
			.sort(([, a], [, b]) => {
				const nameA = a.project?.name || '';
				const nameB = b.project?.name || '';
				return nameA.localeCompare(nameB);
			});

		for (const [key, group] of projectGroups) {
			result.push({ key, project: group.project, sessions: group.sessions });
		}

		// Add ungrouped at the end
		if (ungrouped && ungrouped.sessions.length > 0) {
			result.push({ key: '__ungrouped__', project: null, sessions: ungrouped.sessions });
		}

		return result;
	});

	function isProjectExpanded(key: string): boolean {
		return !collapsedProjects.has(key);
	}

	function toggleProjectGroup(key: string) {
		const next = new Set(collapsedProjects);
		if (next.has(key)) {
			next.delete(key);
		} else {
			next.add(key);
		}
		collapsedProjects = next;
	}

	async function handleDeleteProject(e: MouseEvent, projectId: string) {
		e.stopPropagation();
		if (deletingProjectId === projectId) {
			// Confirm delete
			deleteLoading = true;
			try {
				await appData.deleteProject(projectId);
				await appData.loadSessions();
				deletingProjectId = null;
			} catch (err) {
				console.error('Failed to delete project:', err);
			} finally {
				deleteLoading = false;
			}
		} else {
			deletingProjectId = projectId;
			// Auto-dismiss after 3 seconds
			setTimeout(() => {
				if (deletingProjectId === projectId) {
					deletingProjectId = null;
				}
			}, 3000);
		}
	}

	function cancelDeleteProject(e: MouseEvent) {
		e.stopPropagation();
		deletingProjectId = null;
	}

	// Check if a session is currently streaming (active session or other devices)
	function isStreaming(sessionId: string): boolean {
		// If this is the active session, its isStreaming flag is authoritative
		if ($activeSession.sessionId === sessionId) return $activeSession.isStreaming;
		// For sessions that are not active, use cross-device streaming tracker
		return $streamingSessions.has(sessionId);
	}

	function needsAttention(sessionId: string): boolean {
		return $backgroundPermissions.has(sessionId);
	}

	// Format relative time
	function formatRelativeTime(dateStr: string): string {
		const date = new Date(dateStr);
		const now = new Date();
		const diffMs = now.getTime() - date.getTime();
		const diffMin = Math.floor(diffMs / 60000);
		const diffHr = Math.floor(diffMs / 3600000);
		const diffDay = Math.floor(diffMs / 86400000);

		if (diffMin < 1) return 'now';
		if (diffMin < 60) return `${diffMin}m`;
		if (diffHr < 24) return `${diffHr}h`;
		if (diffDay < 30) return `${diffDay}d`;
		return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}

	// Check if this view is active
	function isActiveView(type: ViewType, sessionId?: string): boolean {
		const view = $activeView;
		if (!view) return false;
		if (type === 'chat' && sessionId) {
			return view.type === 'chat' && view.sessionId === sessionId;
		}
		return view.type === type;
	}

	function isActiveChat(sessionId: string): boolean {
		return isActiveView('chat', sessionId);
	}
</script>

<aside
	class="side-panel"
	class:collapsed={$sidebarCollapsed}
	aria-label="Sidebar navigation"
>
	{#if !$sidebarCollapsed}
		<!-- Collapse toggle (only shown when sidebar is open; header handles collapsed state) -->
		<button
			class="collapse-toggle desktop-collapse"
			onclick={() => layout.toggleSidebar()}
			title="Collapse sidebar"
			aria-expanded="true"
		>
			<PanelLeftClose size={16} strokeWidth={1.5} />
		</button>

		<!-- Mobile close button (visible only on mobile) -->
		<button
			class="collapse-toggle mobile-close-btn"
			onclick={() => layout.toggleSidebar()}
			aria-label="Close sidebar"
		>
			<X size={20} strokeWidth={2} />
			<span class="mobile-close-label">Close</span>
		</button>
		<div class="side-panel-content">
			<!-- New Session Button -->
			<button class="new-session-btn" onclick={onNewSession}>
				<Plus size={16} strokeWidth={2} />
				<span>New session</span>
			</button>

			<!-- Navigation Items -->
			<div class="nav-section">
				<button
					class="section-header"
					onclick={() => navExpanded = !navExpanded}
					aria-expanded={navExpanded}
				>
					<span class="section-title">Pages</span>
					<div class="section-actions">
						{#if navExpanded}
							<ChevronDown size={14} strokeWidth={1.5} />
						{:else}
							<ChevronRight size={14} strokeWidth={1.5} />
						{/if}
					</div>
				</button>

				{#if navExpanded}
					<nav class="nav-items">
						{#each filteredNavItems as item}
							<button
								class="nav-item"
								class:active={isActiveView(item.type)}
								onclick={() => onOpenFeature(item.type)}
							>
								<item.icon size={16} strokeWidth={1.5} />
								<span>{item.label}</span>
							</button>
						{/each}
					</nav>
				{/if}
			</div>

			<!-- Divider -->
			<div class="divider"></div>

			<!-- Recent Sessions -->
			{#if recentSessions.length > 0 && !searchQuery}
				<div class="recent-section">
					{#each recentSessions as session (session.id)}
						<button
							class="session-item"
							class:active={isActiveChat(session.id)}
							onclick={() => onOpenSession(session.id)}
						>
							{#if isStreaming(session.id)}
								<div class="streaming-dot"></div>
							{:else if needsAttention(session.id)}
								<div class="attention-dot"></div>
							{/if}
							<span class="session-title">{session.title || 'Untitled'}</span>
							<span class="session-time">{formatRelativeTime(session.updated_at || session.created_at)}</span>
							<!-- svelte-ignore a11y_no_static_element_interactions -->
							<span
								class="favorite-btn"
								class:is-favorite={session.is_favorite}
								role="button"
								tabindex="0"
								onclick={(e) => handleToggleFavorite(e, session)}
								onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') handleToggleFavorite(e, session); }}
								title={session.is_favorite ? 'Remove from favorites' : 'Add to favorites'}
							>
								<Star size={12} strokeWidth={1.5} fill={session.is_favorite ? 'currentColor' : 'none'} />
							</span>
						</button>
					{/each}
				</div>

				<!-- Divider -->
				<div class="divider"></div>
			{/if}

			<!-- Sessions Section -->
			<div class="sessions-section">
				<button
					class="section-header"
					onclick={() => sessionsExpanded = !sessionsExpanded}
					aria-expanded={sessionsExpanded}
				>
					<span class="section-title">Sessions</span>
					<div class="section-actions">
						{#if sessionsExpanded}
							<ChevronDown size={14} strokeWidth={1.5} />
						{:else}
							<ChevronRight size={14} strokeWidth={1.5} />
						{/if}
					</div>
				</button>

				{#if sessionsExpanded}
					<!-- Search -->
					<div class="search-container">
						<Search size={14} strokeWidth={1.5} class="search-icon" />
						<input
							bind:this={searchInputRef}
							type="text"
							placeholder="Search sessions..."
							bind:value={searchQuery}
							class="search-input"
						/>
						{#if searchQuery}
							<button
								class="search-clear"
								onclick={() => { searchQuery = ''; }}
							>
								<X size={12} strokeWidth={2} />
							</button>
						{/if}
					</div>

					<!-- Session List (grouped by project) -->
					<div class="session-list">
						{#each groupedSessions as group (group.key)}
							<div class="project-group">
								<!-- Project group header -->
								<button
									class="project-group-header"
									onclick={() => toggleProjectGroup(group.key)}
								>
									<div class="project-group-chevron">
										{#if isProjectExpanded(group.key)}
											<ChevronDown size={12} strokeWidth={1.5} />
										{:else}
											<ChevronRight size={12} strokeWidth={1.5} />
										{/if}
									</div>
									<FolderClosed size={13} strokeWidth={1.5} class="project-group-icon" />
									<span class="project-group-name">
										{group.project?.name || 'Ungrouped'}
									</span>
									<span class="project-group-count">{group.sessions.length}</span>

									{#if group.project}
										{#if deletingProjectId === group.project.id}
											<button
												class="project-delete-confirm"
												onclick={(e) => handleDeleteProject(e, group.project!.id)}
												title="Confirm delete"
												disabled={deleteLoading}
											>
												<Trash2 size={12} strokeWidth={2} />
											</button>
											<button
												class="project-delete-cancel"
												onclick={cancelDeleteProject}
												title="Cancel"
											>
												<X size={12} strokeWidth={2} />
											</button>
										{:else}
											<button
												class="project-delete-btn"
												onclick={(e) => handleDeleteProject(e, group.project!.id)}
												title="Delete project"
											>
												<Trash2 size={12} strokeWidth={1.5} />
											</button>
										{/if}
									{/if}
								</button>

								{#if isProjectExpanded(group.key)}
									<div class="project-group-sessions">
										{#each group.sessions as session (session.id)}
											<button
												class="session-item"
												class:active={isActiveChat(session.id)}
												onclick={() => onOpenSession(session.id)}
											>
												{#if isStreaming(session.id)}
													<div class="streaming-dot"></div>
												{/if}
												<span class="session-title">{session.title || 'Untitled'}</span>
												<span class="session-time">{formatRelativeTime(session.updated_at || session.created_at)}</span>
												<!-- svelte-ignore a11y_no_static_element_interactions -->
												<span
													class="favorite-btn"
													class:is-favorite={session.is_favorite}
													role="button"
													tabindex="0"
													onclick={(e) => handleToggleFavorite(e, session)}
													onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') handleToggleFavorite(e, session); }}
													title={session.is_favorite ? 'Remove from favorites' : 'Add to favorites'}
												>
													<Star size={12} strokeWidth={1.5} fill={session.is_favorite ? 'currentColor' : 'none'} />
												</span>
											</button>
										{/each}
									</div>
								{/if}
							</div>
						{:else}
							<p class="empty-message">
								{searchQuery ? 'No sessions found' : 'No sessions yet'}
							</p>
						{/each}
					</div>
				{/if}
			</div>
		</div>
	{/if}
</aside>

<style>
	.side-panel {
		position: relative;
		display: flex;
		flex-direction: column;
		width: var(--sidebar-width, 390px);
		height: 100%;
		background: var(--background);
		border-right: 1px solid var(--border);
		transition: width 200ms cubic-bezier(0.16, 1, 0.3, 1);
		overflow: hidden;
		flex-shrink: 0;
	}

	.side-panel.collapsed {
		width: 0;
		border-right: none;
	}

	/* Collapse toggle button */
	.collapse-toggle {
		width: 28px;
		height: 28px;
		display: flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		margin: 12px 12px 0;
		transition: color 150ms ease, background-color 150ms ease;
	}

	.collapse-toggle:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	/* Desktop: show PanelLeftClose, hide mobile close */
	.desktop-collapse {
		align-self: flex-end;
	}

	.mobile-close-btn {
		display: none;
	}

	/* Mobile: show X close button, hide desktop collapse */
	@media (max-width: 639px) {
		.desktop-collapse {
			display: none;
		}

		.mobile-close-btn {
			display: flex;
			align-items: center;
			gap: 8px;
			width: auto;
			height: 44px;
			padding: 0 12px;
			min-width: 44px;
			-webkit-tap-highlight-color: transparent;
		}

		.mobile-close-label {
			font-size: 0.8125rem;
			font-weight: 500;
			color: var(--muted-foreground);
		}

		.mobile-close-btn:hover .mobile-close-label,
		.mobile-close-btn:active .mobile-close-label {
			color: var(--foreground);
		}
	}

	/* Panel content wrapper */
	.side-panel-content {
		display: flex;
		flex-direction: column;
		flex: 1;
		min-height: 0;
		padding: 12px 0;
		overflow: hidden;
		animation: fadeIn 150ms ease;
	}

	@media (max-width: 1023px) {
		.side-panel-content {
			overflow-y: auto;
			-webkit-overflow-scrolling: touch;
		}
	}

	@keyframes fadeIn {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	/* New Session Button */
	.new-session-btn {
		display: flex;
		align-items: center;
		gap: 10px;
		margin: 0 12px 8px;
		padding: 8px 12px;
		background: transparent;
		border: none;
		border-radius: 8px;
		color: var(--foreground);
		font-size: 0.8125rem;
		font-weight: 500;
		cursor: pointer;
		transition: background-color 150ms ease;
	}

	.new-session-btn:hover {
		background: var(--hover-overlay);
	}

	.new-session-btn :global(svg) {
		color: var(--muted-foreground);
	}

	/* Navigation Section */
	.nav-section {
		padding: 0 8px;
	}

	.nav-items {
		display: flex;
		flex-direction: column;
		gap: 1px;
	}

	.nav-item {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 7px 12px;
		background: transparent;
		border: none;
		border-radius: 7px;
		color: var(--muted-foreground);
		font-size: 0.8125rem;
		font-weight: 400;
		cursor: pointer;
		transition: all 120ms ease;
		text-align: left;
	}

	.nav-item:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.nav-item.active {
		background: var(--selection-bg-emphasis);
		color: var(--selection-text);
		box-shadow: var(--selection-shadow-subtle);
	}

	.nav-item.active :global(svg) {
		color: var(--selection-text);
	}

	/* Divider */
	.divider {
		height: 1px;
		background: var(--border);
		margin: 8px 16px;
		opacity: 0.5;
	}

	/* Recent Sessions */
	.recent-section {
		display: flex;
		flex-direction: column;
		gap: 1px;
		padding: 0 8px;
	}

	/* Sessions Section */
	.sessions-section {
		display: flex;
		flex-direction: column;
		flex: 1;
		min-height: 0;
		padding: 0 8px;
	}

	.section-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 6px 12px;
		background: transparent;
		border: none;
		color: var(--muted-foreground);
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		cursor: pointer;
		transition: color 120ms ease;
	}

	.section-header:hover {
		color: var(--foreground);
	}

	.section-title {
		flex: 1;
		text-align: left;
	}

	.section-actions {
		display: flex;
		align-items: center;
		gap: 4px;
	}

	/* Search */
	.search-container {
		position: relative;
		margin: 4px 4px 6px;
	}

	.search-container :global(.search-icon) {
		position: absolute;
		left: 8px;
		top: 50%;
		transform: translateY(-50%);
		color: var(--muted-foreground);
		pointer-events: none;
	}

	.search-input {
		width: 100%;
		padding: 5px 24px 5px 28px;
		background: var(--input);
		border: 1px solid var(--border);
		border-radius: 6px;
		color: var(--foreground);
		font-size: 0.75rem;
		outline: none;
		transition: border-color 150ms ease, box-shadow 150ms ease;
	}

	.search-input::placeholder {
		color: var(--muted-foreground);
	}

	.search-input:focus {
		border-color: var(--ring);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 15%, transparent);
	}

	.search-clear {
		position: absolute;
		right: 4px;
		top: 50%;
		transform: translateY(-50%);
		display: flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		height: 18px;
		background: transparent;
		border: none;
		border-radius: 4px;
		color: var(--muted-foreground);
		cursor: pointer;
	}

	.search-clear:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	/* Session List */
	.session-list {
		flex: 1;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding-bottom: 12px;
	}

	/* Project Group */
	.project-group {
		display: flex;
		flex-direction: column;
	}

	.project-group-header {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 5px 8px;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: var(--muted-foreground);
		font-size: 0.6875rem;
		font-weight: 600;
		cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
		text-align: left;
		letter-spacing: 0.02em;
	}

	.project-group-header:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	.project-group-chevron {
		display: flex;
		align-items: center;
		flex-shrink: 0;
		opacity: 0.7;
	}

	.project-group-header :global(.project-group-icon) {
		flex-shrink: 0;
		opacity: 0.6;
	}

	.project-group-name {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.project-group-count {
		flex-shrink: 0;
		font-size: 0.625rem;
		font-weight: 500;
		color: var(--muted-foreground);
		opacity: 0.6;
		background: var(--hover-overlay);
		padding: 1px 5px;
		border-radius: 8px;
	}

	/* Project delete button */
	.project-delete-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 20px;
		padding: 0;
		background: transparent;
		border: none;
		border-radius: 4px;
		color: var(--muted-foreground);
		cursor: pointer;
		opacity: 0;
		flex-shrink: 0;
		transition: opacity 150ms ease, color 150ms ease, background-color 150ms ease;
	}

	.project-group-header:hover .project-delete-btn {
		opacity: 1;
	}

	.project-delete-btn:hover {
		color: var(--destructive, #ef4444);
		background: color-mix(in oklch, var(--destructive, #ef4444) 12%, transparent);
	}

	/* Confirm/cancel delete buttons */
	.project-delete-confirm {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 22px;
		height: 22px;
		padding: 0;
		background: color-mix(in oklch, var(--destructive, #ef4444) 15%, transparent);
		border: 1px solid var(--destructive, #ef4444);
		border-radius: 4px;
		color: var(--destructive, #ef4444);
		cursor: pointer;
		flex-shrink: 0;
		transition: background-color 120ms ease;
		animation: fadeIn 150ms ease;
	}

	.project-delete-confirm:hover {
		background: color-mix(in oklch, var(--destructive, #ef4444) 25%, transparent);
	}

	.project-delete-confirm:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.project-delete-cancel {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 22px;
		height: 22px;
		padding: 0;
		background: transparent;
		border: 1px solid var(--border);
		border-radius: 4px;
		color: var(--muted-foreground);
		cursor: pointer;
		flex-shrink: 0;
		transition: color 120ms ease, background-color 120ms ease;
		animation: fadeIn 150ms ease;
	}

	.project-delete-cancel:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	/* Touch devices: always show delete button */
	@media (pointer: coarse) {
		.project-delete-btn {
			opacity: 0.5;
		}
	}

	@media (max-width: 639px) {
		.project-delete-btn {
			opacity: 0.5;
			min-width: 32px;
			min-height: 32px;
		}

		.project-delete-confirm,
		.project-delete-cancel {
			min-width: 32px;
			min-height: 32px;
		}
	}

	.project-group-sessions {
		display: flex;
		flex-direction: column;
		gap: 1px;
		padding-left: 8px;
	}

	/* Session Item */
	.session-item {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 7px 12px;
		background: transparent;
		border: none;
		border-radius: 7px;
		color: var(--foreground);
		font-size: 0.8125rem;
		cursor: pointer;
		text-align: left;
		transition: background-color 120ms ease;
		position: relative;
	}

	.session-item:hover {
		background: var(--hover-overlay);
	}

	.session-item.active {
		background: var(--selection-bg);
	}

	.session-item.active .session-title {
		color: var(--selection-text);
		font-weight: 500;
	}

	.session-item :global(.session-icon) {
		flex-shrink: 0;
		color: var(--muted-foreground);
		opacity: 0.6;
	}

	.session-title {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 400;
	}

	.session-time {
		flex-shrink: 0;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		opacity: 0.7;
	}

	/* Favorite/pin button */
	.favorite-btn {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 22px;
		height: 22px;
		padding: 0;
		background: transparent;
		border: none;
		border-radius: 4px;
		color: var(--muted-foreground);
		cursor: pointer;
		opacity: 0;
		transition: opacity 150ms ease, color 150ms ease, background-color 150ms ease;
	}

	.favorite-btn.is-favorite {
		opacity: 1;
		color: var(--warning, #eab308);
	}

	.session-item:hover .favorite-btn {
		opacity: 1;
	}

	.favorite-btn:hover {
		color: var(--warning, #eab308);
		background: var(--hover-overlay);
	}

	/* On touch devices and mobile: always show the star */
	@media (pointer: coarse) {
		.favorite-btn {
			opacity: 0.6;
		}

		.favorite-btn.is-favorite {
			opacity: 1;
		}
	}

	@media (max-width: 639px) {
		.favorite-btn {
			opacity: 0.6;
			min-width: 32px;
			min-height: 32px;
		}

		.favorite-btn.is-favorite {
			opacity: 1;
		}
	}

	/* Streaming indicator */
	.streaming-dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--primary);
		flex-shrink: 0;
		animation: pulse 2s ease-in-out infinite;
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.4; }
	}

	/* Needs-attention indicator (pending permissions) */
	.attention-dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--warning, #f59e0b);
		flex-shrink: 0;
		animation: attention-pulse 1.5s ease-in-out infinite;
	}

	@keyframes attention-pulse {
		0%, 100% { opacity: 1; transform: scale(1); }
		50% { opacity: 0.6; transform: scale(1.3); }
	}

	.empty-message {
		padding: 12px;
		text-align: center;
		color: var(--muted-foreground);
		font-size: 0.75rem;
	}

	/* Mobile: let .side-panel-content handle all scrolling */
	@media (max-width: 1023px) {
		.sessions-section {
			flex: none;
		}
		.session-list {
			overflow-y: visible;
			flex: none;
		}
	}

	/* Reduced motion */
	@media (prefers-reduced-motion: reduce) {
		.side-panel {
			transition: none;
		}
		.streaming-dot {
			animation: none;
		}
		.side-panel-content {
			animation: none;
		}
	}
</style>
