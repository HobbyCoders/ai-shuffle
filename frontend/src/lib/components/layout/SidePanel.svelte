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
	import BrushCleaning from 'lucide-svelte/icons/brush-cleaning';
	import { layout, activeView, sidebarCollapsed } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import { appData, sessions, projects, apiUserSessions } from '$lib/stores/app-data.svelte';
	import { activeSession, streamingSessions, backgroundPermissions } from '$lib/stores/active-session';
	import { toggleSessionFavorite } from '$lib/api/client';
	import type { Session } from '$lib/api/client';
	import type { Project } from '$lib/stores/types';
	import { toast } from '$lib/stores/toast';

	interface Props {
		isAdmin?: boolean;
		/** Slim mode: hide the "Pages" nav + footer (owned by the global top bar) */
		slim?: boolean;
		onNewSession: () => void;
		onNewSessionForProject?: (projectId: string) => void;
		onOpenSession: (sessionId: string) => void;
		onOpenFeature: (type: ViewType) => void;
	}

	let {
		isAdmin = false,
		slim = false,
		onNewSession,
		onNewSessionForProject,
		onOpenSession,
		onOpenFeature
	}: Props = $props();

	// Search state
	let searchQuery = $state('');
	let searchInputRef = $state<HTMLInputElement | null>(null);

	// Load persisted sidebar collapse state
	function loadSidebarState(): Record<string, unknown> | null {
		try {
			const saved = localStorage.getItem('sidebar-collapse-state');
			if (saved) return JSON.parse(saved);
		} catch { /* ignore */ }
		return null;
	}
	const _saved = loadSidebarState();

	let navExpanded = $state((_saved?.navExpanded as boolean) ?? true);
	let recentExpanded = $state((_saved?.recentExpanded as boolean) ?? true);
	let sessionsExpanded = $state((_saved?.sessionsExpanded as boolean) ?? true);
	let apiUsersExpanded = $state((_saved?.apiUsersExpanded as boolean) ?? true);

	// Track which project groups are COLLAPSED (empty set = all expanded by default)
	// This avoids needing a $effect to sync with project list, preventing infinite loops
	let collapsedProjects = $state<Set<string>>(new Set((_saved?.collapsedProjects as string[]) ?? []));
	let collapsedApiUsers = $state<Set<string>>(new Set((_saved?.collapsedApiUsers as string[]) ?? []));

	// Persist sidebar collapse state
	$effect(() => {
		const state = {
			navExpanded,
			recentExpanded,
			sessionsExpanded,
			apiUsersExpanded,
			collapsedProjects: [...collapsedProjects],
			collapsedApiUsers: [...collapsedApiUsers]
		};
		localStorage.setItem('sidebar-collapse-state', JSON.stringify(state));
	});

	// Track project being deleted (for confirmation)
	let deletingProjectId = $state<string | null>(null);
	let deleteLoading = $state(false);

	// Track session being deleted (for confirmation)
	let deletingSessionId = $state<string | null>(null);
	let deleteSessionLoading = $state(false);

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
		{ type: 'file-browser', label: 'Files', icon: FolderOpen },
		{ type: 'plugins', label: 'Plugins', icon: Puzzle },
		{ type: 'mcp', label: 'MCP Servers', icon: Server },
		{ type: 'subagent', label: 'Subagents', icon: Bot },
		{ type: 'notes', label: 'Notes', icon: StickyNote },
		{ type: 'hooks', label: 'Hooks', icon: Webhook },
		{ type: 'automations', label: 'Automations', icon: CalendarClock, adminOnly: true },
		{ type: 'profile', label: 'Profiles', icon: Users },
		{ type: 'project', label: 'Projects', icon: FolderKanban }
	];

	// Footer bar items (icon-only with tooltips)
	const footerItems: { type: ViewType; label: string; icon: typeof Settings; adminOnly?: boolean }[] = [
		{ type: 'docs', label: 'Docs', icon: BookOpen },
		{ type: 'stats', label: 'Stats', icon: BarChart3 },
		{ type: 'settings', label: 'Settings', icon: Settings, adminOnly: true }
	];

	const filteredFooterItems = $derived(
		footerItems.filter((item) => !item.adminOnly || isAdmin)
	);

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
			toast.push('Failed to update favorite', { tone: 'error' });
		}
	}

	const activeId = $derived($activeSession.sessionId);

	const recentSessions = $derived(
		sortWithFavorites($sessions).slice(0, 10)
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
				toast.push('Failed to delete project', { tone: 'error' });
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

	// Delete a session with confirmation
	async function handleDeleteSession(e: MouseEvent | KeyboardEvent, sessionId: string) {
		e.stopPropagation();
		if (deletingSessionId === sessionId) {
			// Confirm delete
			deleteSessionLoading = true;
			try {
				// If deleting the active session, switch to new chat first
				if (activeId === sessionId) {
					onNewSession();
				}
				await appData.deleteSession(sessionId);
			} catch (err) {
				console.error('Failed to delete session:', err);
				toast.push('Failed to delete session', { tone: 'error' });
			} finally {
				deleteSessionLoading = false;
				deletingSessionId = null;
			}
		} else {
			deletingSessionId = sessionId;
			// Auto-dismiss after 3 seconds
			setTimeout(() => {
				if (deletingSessionId === sessionId) {
					deletingSessionId = null;
				}
			}, 3000);
		}
	}

	function cancelDeleteSession(e: MouseEvent | KeyboardEvent) {
		e.stopPropagation();
		deletingSessionId = null;
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

	function formatCost(cost: number): string {
		if (cost < 0.01) return '<$0.01';
		return `$${cost.toFixed(2)}`;
	}

	function formatSessionDuration(createdAt: string, updatedAt: string): string {
		const start = new Date(createdAt);
		const end = new Date(updatedAt);
		const diffMs = end.getTime() - start.getTime();
		if (diffMs < 60000) return '<1m';
		const mins = Math.floor(diffMs / 60000);
		if (mins < 60) return `${mins}m`;
		const hrs = Math.floor(mins / 60);
		const remainMins = mins % 60;
		if (hrs < 24) return remainMins > 0 ? `${hrs}h ${remainMins}m` : `${hrs}h`;
		const days = Math.floor(hrs / 24);
		return `${days}d`;
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

			{#if !slim}
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
			{/if}

			<!-- Recent Sessions -->
			{#if recentSessions.length > 0 && !searchQuery}
				<div class="sessions-section">
					<button
						class="section-header"
						onclick={() => recentExpanded = !recentExpanded}
						aria-expanded={recentExpanded}
					>
						<span class="section-title">Recent</span>
						<div class="section-actions">
							{#if recentExpanded}
								<ChevronDown size={14} strokeWidth={1.5} />
							{:else}
								<ChevronRight size={14} strokeWidth={1.5} />
							{/if}
						</div>
					</button>

					{#if recentExpanded}
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
									<div class="session-info">
										<span class="session-title">{session.title || 'Untitled'}</span>
										{#if session.total_cost_usd > 0}
											<span class="session-meta">
												{formatCost(session.total_cost_usd)} · {formatSessionDuration(session.created_at, session.updated_at || session.created_at)}{#if session.turn_count > 0} · {session.turn_count} {session.turn_count === 1 ? 'turn' : 'turns'}{/if}
											</span>
										{/if}
									</div>
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
									{#if deletingSessionId === session.id}
										<span
											class="session-delete-confirm"
											class:is-disabled={deleteSessionLoading}
											role="button"
											tabindex="0"
											aria-disabled={deleteSessionLoading}
											onclick={(e) => { if (!deleteSessionLoading) handleDeleteSession(e, session.id); }}
											onkeydown={(e) => { if ((e.key === 'Enter' || e.key === ' ') && !deleteSessionLoading) handleDeleteSession(e, session.id); }}
											title="Confirm delete"
										>
											<BrushCleaning size={11} strokeWidth={2} />
										</span>
										<span
											class="session-delete-cancel"
											role="button"
											tabindex="0"
											onclick={(e) => cancelDeleteSession(e)}
											onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') cancelDeleteSession(e); }}
											title="Cancel"
										>
											<X size={11} strokeWidth={2} />
										</span>
									{:else}
										<span
											class="session-delete-btn"
											role="button"
											tabindex="0"
											onclick={(e) => handleDeleteSession(e, session.id)}
											onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') handleDeleteSession(e, session.id); }}
											title="Clear session"
										>
											<BrushCleaning size={11} strokeWidth={1.5} />
										</span>
									{/if}
								</button>
							{/each}
						</div>
					{/if}
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
							autocomplete="off"
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
								<div class="project-group-header" class:no-actions={!group.project} role="group">
									<button
										class="project-group-toggle"
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
									</button>

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
												class="project-new-session-btn"
												onclick={(e) => { e.stopPropagation(); onNewSessionForProject?.(group.project!.id); }}
												title="New chat in this project"
											>
												<Plus size={12} strokeWidth={1.5} />
											</button>
											<button
												class="project-delete-btn"
												onclick={(e) => handleDeleteProject(e, group.project!.id)}
												title="Delete project"
											>
												<Trash2 size={12} strokeWidth={1.5} />
											</button>
										{/if}
									{/if}
								</div>

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
												<div class="session-info">
													<span class="session-title">{session.title || 'Untitled'}</span>
													{#if session.total_cost_usd > 0}
														<span class="session-meta">
															{formatCost(session.total_cost_usd)} · {formatSessionDuration(session.created_at, session.updated_at || session.created_at)}{#if session.turn_count > 0} · {session.turn_count} {session.turn_count === 1 ? 'turn' : 'turns'}{/if}
														</span>
													{/if}
												</div>
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
												{#if deletingSessionId === session.id}
													<span
														class="session-delete-confirm"
														class:is-disabled={deleteSessionLoading}
														role="button"
														tabindex="0"
														aria-disabled={deleteSessionLoading}
														onclick={(e) => { if (!deleteSessionLoading) handleDeleteSession(e, session.id); }}
														onkeydown={(e) => { if ((e.key === 'Enter' || e.key === ' ') && !deleteSessionLoading) handleDeleteSession(e, session.id); }}
														title="Confirm delete"
													>
														<BrushCleaning size={11} strokeWidth={2} />
													</span>
													<span
														class="session-delete-cancel"
														role="button"
														tabindex="0"
														onclick={(e) => cancelDeleteSession(e)}
														onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') cancelDeleteSession(e); }}
														title="Cancel"
													>
														<X size={11} strokeWidth={2} />
													</span>
												{:else}
													<span
														class="session-delete-btn"
														role="button"
														tabindex="0"
														onclick={(e) => handleDeleteSession(e, session.id)}
														onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') handleDeleteSession(e, session.id); }}
														title="Clear session"
													>
														<BrushCleaning size={11} strokeWidth={1.5} />
													</span>
												{/if}
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
													<div class="session-info">
														<span class="session-title">{session.title || 'Untitled'}</span>
														{#if session.total_cost_usd > 0}
															<span class="session-meta">
																{formatCost(session.total_cost_usd)} · {formatSessionDuration(session.created_at, session.updated_at || session.created_at)}{#if session.turn_count > 0} · {session.turn_count} {session.turn_count === 1 ? 'turn' : 'turns'}{/if}
															</span>
														{/if}
													</div>
													<span class="session-time">{formatRelativeTime(session.updated_at || session.created_at)}</span>
													{#if deletingSessionId === session.id}
														<span
															class="session-delete-confirm"
															class:is-disabled={deleteSessionLoading}
															role="button"
															tabindex="0"
															aria-disabled={deleteSessionLoading}
															onclick={(e) => { if (!deleteSessionLoading) handleDeleteSession(e, session.id); }}
															onkeydown={(e) => { if ((e.key === 'Enter' || e.key === ' ') && !deleteSessionLoading) handleDeleteSession(e, session.id); }}
															title="Confirm delete"
														>
															<BrushCleaning size={11} strokeWidth={2} />
														</span>
														<span
															class="session-delete-cancel"
															role="button"
															tabindex="0"
															onclick={(e) => cancelDeleteSession(e)}
															onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') cancelDeleteSession(e); }}
															title="Cancel"
														>
															<X size={11} strokeWidth={2} />
														</span>
													{:else}
														<span
															class="session-delete-btn"
															role="button"
															tabindex="0"
															onclick={(e) => handleDeleteSession(e, session.id)}
															onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') handleDeleteSession(e, session.id); }}
															title="Clear session"
														>
															<BrushCleaning size={11} strokeWidth={1.5} />
														</span>
													{/if}
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

		<!-- Footer bar: icon-only shortcuts -->
		{#if !slim}
		<div class="sidebar-footer">
			{#each filteredFooterItems as item}
				<button
					class="footer-icon-btn"
					class:active={isActiveView(item.type)}
					onclick={() => onOpenFeature(item.type)}
					title={item.label}
				>
					<item.icon size={18} strokeWidth={1.5} />
				</button>
			{/each}
		</div>
		{/if}
	{/if}
</aside>

<style>
	.side-panel {
		position: relative;
		display: flex;
		flex-direction: column;
		width: var(--sidebar-width, 390px);
		height: 100%;
		background: color-mix(in oklch, var(--muted) 40%, var(--background));
		border-right: 1px solid var(--sidebar-border, var(--border));
		transition:
			width 350ms cubic-bezier(0.16, 1, 0.3, 1),
			transform 350ms cubic-bezier(0.16, 1, 0.3, 1),
			opacity 250ms ease,
			box-shadow 350ms ease;
		overflow: hidden;
		flex-shrink: 0;
		z-index: 1;
		transform-origin: left center;
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

	/* Desktop: show PanelLeftClose, hide mobile close — .desktop-collapse visibility managed by media queries */

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
		opacity: 1;
		transition: opacity 200ms ease 50ms;
	}

	.side-panel.collapsed .side-panel-content {
		opacity: 0;
		transition: opacity 100ms ease;
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
		gap: 8px;
		padding: 6px 12px;
		background: transparent;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		color: var(--foreground);
		font-size: 0.8125rem;
		font-weight: 500;
		cursor: pointer;
		transition: background-color 150ms ease, border-color 150ms ease;
	}

	.new-session-btn:hover {
		background: var(--hover-overlay);
		border-color: var(--foreground);
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
		padding: 6px 12px;
		background: transparent;
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
	}

	.nav-item.active :global(svg) {
		color: var(--selection-text);
	}

	/* Divider — spacing only, no visible line */
	.divider {
		height: 0;
		margin: 6px 0;
	}

	/* Recent Sessions */
	.recent-section {
		display: flex;
		flex-direction: column;
		gap: 1px;
		padding: 0 4px;
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
		background: transparent;
		border: 1px solid var(--border);
		border-radius: var(--radius);
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
		gap: 2px;
		padding: 0 4px 12px;
	}

	/* Project Group */
	.project-group {
		display: flex;
		flex-direction: column;
	}

	.project-group-header {
		display: flex;
		align-items: center;
		gap: 0;
		padding: 0;
		color: var(--muted-foreground);
		font-size: 0.6875rem;
		font-weight: 600;
		letter-spacing: 0.02em;
		transition: color 120ms ease, background-color 120ms ease;
	}

	.project-group-header:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	.project-group-toggle {
		display: flex;
		align-items: center;
		gap: 6px;
		flex: 1;
		min-width: 0;
		padding: 8px 12px;
		background: transparent;
		border: none;
		border-radius: 0;
		color: inherit;
		font-size: inherit;
		font-weight: inherit;
		letter-spacing: inherit;
		cursor: pointer;
		text-align: left;
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
		min-width: 1.5rem;
		padding: 1px 4px;
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		text-align: center;
		box-sizing: border-box;
	}

	.project-group-header.no-actions {
		padding-right: 28px;
	}

	/* Project new session button */
	.project-new-session-btn {
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

	.project-group-header:hover .project-new-session-btn {
		opacity: 1;
	}

	.project-new-session-btn:hover {
		color: var(--primary, #3b82f6);
		background: color-mix(in oklch, var(--primary, #3b82f6) 12%, transparent);
	}

	/* Project delete button */
	.project-delete-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 20px;
		margin-right: 8px;
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
		margin-left: 2px;
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
		margin-right: 8px;
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

	/* Touch devices: always show action buttons */
	@media (pointer: coarse) {
		.project-new-session-btn,
		.project-delete-btn {
			opacity: 0.5;
		}
	}

	@media (max-width: 639px) {
		.project-new-session-btn,
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

	/* Sessions inside project groups */
	.project-group-sessions .session-item {
		padding-left: 28px;
	}

	/* Session Item */
	.session-item {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 7px 12px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius) - 2px);
		border-left: 3px solid transparent;
		color: var(--foreground);
		font-size: 0.8125rem;
		cursor: pointer;
		text-align: left;
		transition: background-color 120ms ease, border-color 120ms ease;
		position: relative;
	}

	.session-item:hover {
		background: var(--hover-overlay);
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

	.session-info {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 1px;
	}

	.session-title {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 400;
	}

	.session-meta {
		font-size: 0.625rem;
		color: var(--muted-foreground);
		opacity: 0.55;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
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

	/* Session delete button */
	.session-delete-btn {
		flex-shrink: 0;
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
		transition: opacity 150ms ease, color 150ms ease, background-color 150ms ease;
	}

	.session-item:hover .session-delete-btn {
		opacity: 1;
	}

	.session-delete-btn:hover {
		color: var(--destructive, #ef4444);
		background: color-mix(in oklch, var(--destructive, #ef4444) 12%, transparent);
	}

	.session-delete-confirm {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 20px;
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

	.session-delete-confirm:hover {
		background: color-mix(in oklch, var(--destructive, #ef4444) 25%, transparent);
	}

	.session-delete-confirm.is-disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.session-delete-confirm.is-disabled:hover {
		background: color-mix(in oklch, var(--destructive, #ef4444) 15%, transparent);
	}

	.session-delete-cancel {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 20px;
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

	.session-delete-cancel:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	/* Touch devices: always show session delete */
	@media (pointer: coarse) {
		.session-delete-btn {
			opacity: 0.5;
		}
	}

	@media (max-width: 639px) {
		.session-delete-btn {
			opacity: 0.5;
			min-width: 32px;
			min-height: 32px;
		}

		.session-delete-confirm,
		.session-delete-cancel {
			min-width: 32px;
			min-height: 32px;
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

	/* Sidebar Footer */
	.sidebar-footer {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 4px;
		padding: 8px 12px;
		flex-shrink: 0;
		border-top: 1px solid var(--border);
	}

	.footer-icon-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius) - 2px);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
		position: relative;
	}

	.footer-icon-btn:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.footer-icon-btn.active {
		background: var(--selection-bg-emphasis);
		color: var(--selection-text);
	}

	/* Mobile: larger tap targets */
	@media (max-width: 639px) {
		.footer-icon-btn {
			width: 44px;
			height: 44px;
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
