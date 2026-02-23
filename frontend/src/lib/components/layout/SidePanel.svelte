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
		KeyRound,
		FolderKanban,
		CalendarClock,
		BookOpen
	} from 'lucide-svelte';
	import { layout, activeView, sidebarCollapsed } from '$lib/stores/layout';
	import type { ViewType } from '$lib/stores/layout';
	import { appData, sessions, projects, apiUserSessions } from '$lib/stores/app-data';
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

	// API Users section
	let apiUsersExpanded = $state(true);
	let collapsedApiUsers = $state<Set<string>>(new Set());

	// Group API user sessions by user name
	interface ApiUserGroup {
		userName: string;
		sessions: Session[];
	}

	const groupedApiUserSessions = $derived.by((): ApiUserGroup[] => {
		const groups = new Map<string, Session[]>();

		for (const session of $apiUserSessions) {
			const userName = session.api_user_name || 'Unknown User';
			if (!groups.has(userName)) {
				groups.set(userName, []);
			}
			groups.get(userName)!.push(session);
		}

		return [...groups.entries()]
			.sort(([a], [b]) => a.localeCompare(b))
			.map(([userName, sessions]) => ({ userName, sessions }));
	});

	function isApiUserExpanded(userName: string): boolean {
		return !collapsedApiUsers.has(userName);
	}

	function toggleApiUserGroup(userName: string) {
		const next = new Set(collapsedApiUsers);
		if (next.has(userName)) {
			next.delete(userName);
		} else {
			next.add(userName);
		}
		collapsedApiUsers = next;
	}

	// Navigation items
	const navItems: { type: ViewType; label: string; icon: typeof Settings; adminOnly?: boolean }[] = [
		{ type: 'docs', label: 'Docs', icon: BookOpen },
		{ type: 'file-browser', label: 'Files', icon: FolderOpen },
		{ type: 'plugins', label: 'Plugins', icon: Puzzle },
		{ type: 'mcp', label: 'MCP Servers', icon: Server },
		{ type: 'subagent', label: 'Subagents', icon: Bot },
		{ type: 'notes', label: 'Notes', icon: StickyNote },
		{ type: 'hooks', label: 'Hooks', icon: Webhook },
		{ type: 'automations', label: 'Automations', icon: CalendarClock, adminOnly: true },
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

	const activeId = $derived($activeSession.sessionId);

	const recentSessions = $derived(
		sortWithFavorites($sessions).slice(0, 5)
	);

	const filteredSessions = $derived.by(() => {
		if (!searchQuery.trim()) return sortWithFavorites($sessions);
		const q = searchQuery.toLowerCase();
		return sortWithFavorites(
			$sessions.filter(
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
		<!-- Mobile close button (visible only on mobile) -->
		<button
			class="collapse-toggle mobile-close-btn"
			onclick={() => layout.toggleSidebar()}
			aria-label="Close sidebar"
		>
			<X size={20} strokeWidth={2} />
			<span class="mobile-close-label">Close</span>
		</button>

		<!-- Header: New Session + Collapse toggle -->
		<div class="sidebar-header">
			<button class="new-session-btn" onclick={onNewSession}>
				<Plus size={16} strokeWidth={2} />
				<span>New session</span>
			</button>
			<button
				class="collapse-toggle desktop-collapse"
				onclick={() => layout.toggleSidebar()}
				title="Collapse sidebar"
				aria-expanded="true"
			>
				<PanelLeftClose size={16} strokeWidth={1.5} />
			</button>
		</div>

		<div class="side-panel-content">

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

			<!-- API Users Section -->
			{#if groupedApiUserSessions.length > 0}
				<div class="divider"></div>

				<div class="sessions-section">
					<button
						class="section-header"
						onclick={() => apiUsersExpanded = !apiUsersExpanded}
						aria-expanded={apiUsersExpanded}
					>
						<span class="section-title">API Users</span>
						<div class="section-actions">
							{#if apiUsersExpanded}
								<ChevronDown size={14} strokeWidth={1.5} />
							{:else}
								<ChevronRight size={14} strokeWidth={1.5} />
							{/if}
						</div>
					</button>

					{#if apiUsersExpanded}
						<div class="session-list">
							{#each groupedApiUserSessions as group (group.userName)}
								<div class="project-group">
									<button
										class="project-group-header"
										onclick={() => toggleApiUserGroup(group.userName)}
									>
										<div class="project-group-chevron">
											{#if isApiUserExpanded(group.userName)}
												<ChevronDown size={12} strokeWidth={1.5} />
											{:else}
												<ChevronRight size={12} strokeWidth={1.5} />
											{/if}
										</div>
										<Users size={13} strokeWidth={1.5} class="project-group-icon" />
										<span class="project-group-name">
											{group.userName}
										</span>
										<span class="project-group-count">{group.sessions.length}</span>
									</button>

									{#if isApiUserExpanded(group.userName)}
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
												</button>
											{/each}
										</div>
									{/if}
								</div>
							{/each}
						</div>
					{/if}
				</div>
			{/if}
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
		background: color-mix(in oklch, var(--background) 85%, black);
		border-right: 1px solid var(--sidebar-border, var(--border));
		box-shadow: 4px 0 16px -4px color-mix(in oklch, var(--foreground) 8%, transparent);
		transition: width 200ms cubic-bezier(0.16, 1, 0.3, 1);
		overflow: hidden;
		flex-shrink: 0;
		z-index: 1;
	}

	.side-panel.collapsed {
		width: 0;
		border-right: none;
		box-shadow: none;
	}

	/* Sidebar header row */
	.sidebar-header {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 12px 12px 0;
		flex-shrink: 0;
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
		border-radius: calc(var(--radius) - 4px);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 150ms ease, background-color 150ms ease;
	}

	.collapse-toggle:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	/* Desktop: show PanelLeftClose, hide mobile close */
	.desktop-collapse {
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
		padding: 8px 0;
		overflow-y: auto;
		-webkit-overflow-scrolling: touch;
		animation: fadeIn 150ms ease;
		scrollbar-width: thin;
		scrollbar-color: var(--scrollbar-thumb) transparent;
	}

	.side-panel-content::-webkit-scrollbar {
		width: 6px;
	}

	.side-panel-content::-webkit-scrollbar-track {
		background: transparent;
	}

	.side-panel-content::-webkit-scrollbar-thumb {
		background: var(--scrollbar-thumb);
		border-radius: 3px;
	}

	.side-panel-content::-webkit-scrollbar-thumb:hover {
		background: var(--scrollbar-thumb-hover);
	}

	@keyframes fadeIn {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	/* New Session Button */
	.new-session-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		flex: 1;
		min-width: 0;
		gap: 10px;
		padding: 8px 12px;
		background: var(--primary);
		border: none;
		border-radius: var(--radius);
		color: var(--primary-foreground);
		font-size: 0.8125rem;
		font-weight: 600;
		cursor: pointer;
		box-shadow: var(--shadow-s);
		transition: opacity 150ms ease, box-shadow 150ms ease;
	}

	.new-session-btn:hover {
		opacity: 0.9;
		box-shadow: var(--shadow-m);
	}

	.new-session-btn :global(svg) {
		color: var(--primary-foreground);
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
		background: color-mix(in oklch, var(--background) 60%, var(--glass, transparent));
		border: none;
		border-radius: calc(var(--radius) - 2px);
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
		box-shadow: var(--shadow-s);
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
		gap: 2px;
		padding: 0 12px;
	}

	/* Sessions Section */
	.sessions-section {
		display: flex;
		flex-direction: column;
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
		padding: 6px 24px 6px 28px;
		background: color-mix(in oklch, var(--background) 60%, var(--glass, transparent));
		border: 1px solid var(--sidebar-border, var(--border));
		border-radius: var(--radius);
		color: var(--foreground);
		font-size: 0.75rem;
		outline: none;
		box-shadow: var(--shadow-s);
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
		border-radius: calc(var(--radius) - 4px);
		color: var(--muted-foreground);
		cursor: pointer;
	}

	.search-clear:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	/* Session List */
	.session-list {
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 0 4px 12px;
	}

	/* Project Group */
	.project-group {
		display: flex;
		flex-direction: column;
		background: color-mix(in oklch, var(--background) 60%, var(--glass, transparent));
		border-radius: var(--radius);
		box-shadow: var(--shadow-s);
		overflow: clip;
	}

	.project-group-header {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 12px;
		background: transparent;
		border: none;
		border-radius: 0;
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
		font-weight: 600;
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		padding: 1px 7px;
		border-radius: 9999px;
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
		border-radius: calc(var(--radius) - 4px);
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
		border-radius: calc(var(--radius) - 4px);
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
		border-radius: calc(var(--radius) - 4px);
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
		gap: 0;
		padding-left: 0;
	}

	/* Sessions inside project group cards: reset card styling, use separator borders */
	.project-group-sessions .session-item {
		background: transparent;
		box-shadow: none;
		border-radius: 0;
		border-left: 3px solid transparent;
		border-top: 1px solid var(--sidebar-border, var(--border));
		padding: 8px 12px;
	}

	.project-group-sessions .session-item:hover {
		background: var(--hover-overlay);
		box-shadow: none;
	}

	.project-group-sessions .session-item.active {
		background: var(--selection-bg);
		border-left-color: var(--primary);
	}

	/* Session Item */
	.session-item {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 12px;
		background: color-mix(in oklch, var(--background) 60%, var(--glass, transparent));
		border: none;
		border-radius: calc(var(--radius) - 2px);
		border-left: 3px solid transparent;
		color: var(--foreground);
		font-size: 0.8125rem;
		cursor: pointer;
		text-align: left;
		box-shadow: var(--shadow-s);
		transition: background-color 120ms ease, border-color 120ms ease, box-shadow 120ms ease;
		position: relative;
	}

	.session-item:hover {
		background: var(--hover-overlay);
		box-shadow: var(--shadow-m);
	}

	.session-item.active {
		background: var(--selection-bg);
		border-left-color: var(--primary);
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
		border-radius: calc(var(--radius) - 4px);
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

	/* Mobile: no extra overrides needed, side-panel-content handles all scrolling */

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
