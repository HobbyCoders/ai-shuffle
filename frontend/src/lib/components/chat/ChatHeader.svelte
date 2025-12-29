<script lang="ts">
	/**
	 * ChatHeader - Context usage indicator, profile selector, and project selector
	 *
	 * Features right-click context menus for profiles and projects with:
	 * - Full CRUD operations
	 * - Group management
	 * - Quick card access
	 */
	import { createEventDispatcher } from 'svelte';
	import {
		tabs,
		profiles,
		projects,
		type ChatTab,
		type Project
	} from '$lib/stores/tabs';
	import type { Profile } from '$lib/api/client';
	import { isAdmin, apiUser } from '$lib/stores/auth';
	import { groups, organizeByGroups } from '$lib/stores/groups';
	import DropdownContextMenu from './DropdownContextMenu.svelte';

	interface Props {
		tab: ChatTab;
		compact?: boolean;
	}

	let { tab, compact = false }: Props = $props();

	const dispatch = createEventDispatcher<{
		openProfileCard: { editId?: string };
		openProjectCard: { editId?: string };
	}>();

	function formatTokenCount(count: number): string {
		if (count >= 1000000) {
			return `${(count / 1000000).toFixed(1)}M`;
		}
		if (count >= 1000) {
			return `${(count / 1000).toFixed(1)}k`;
		}
		return count.toString();
	}

	function setTabProfile(profileId: string) {
		tabs.setTabProfile(tab.id, profileId);
	}

	function setTabProject(projectId: string) {
		tabs.setTabProject(tab.id, projectId);
	}

	// Context menu state for profiles
	let profileContextMenu = $state<{
		show: boolean;
		x: number;
		y: number;
		profile: Profile | null;
	}>({ show: false, x: 0, y: 0, profile: null });

	// Context menu state for projects
	let projectContextMenu = $state<{
		show: boolean;
		x: number;
		y: number;
		project: Project | null;
	}>({ show: false, x: 0, y: 0, project: null });

	function handleProfileContextMenu(e: MouseEvent, profile: Profile) {
		e.preventDefault();
		e.stopPropagation();
		profileContextMenu = {
			show: true,
			x: e.clientX,
			y: e.clientY,
			profile
		};
	}

	function handleProjectContextMenu(e: MouseEvent, project: Project) {
		e.preventDefault();
		e.stopPropagation();
		projectContextMenu = {
			show: true,
			x: e.clientX,
			y: e.clientY,
			project
		};
	}

	function closeProfileContextMenu() {
		profileContextMenu = { show: false, x: 0, y: 0, profile: null };
	}

	function closeProjectContextMenu() {
		projectContextMenu = { show: false, x: 0, y: 0, project: null };
	}

	// Profile context menu handlers
	function handleProfileSelect(e: CustomEvent<{ id: string }>) {
		setTabProfile(e.detail.id);
	}

	function handleProfileEdit(e: CustomEvent<{ id: string }>) {
		dispatch('openProfileCard', { editId: e.detail.id });
	}

	async function handleProfileDuplicate(e: CustomEvent<{ id: string }>) {
		const profile = $profiles.find(p => p.id === e.detail.id);
		if (!profile) return;

		const newId = `${profile.id}-copy-${Date.now().toString(36)}`;
		const newName = `${profile.name} (Copy)`;

		try {
			await tabs.createProfile({
				id: newId,
				name: newName,
				description: profile.description,
				config: profile.config || {}
			});
		} catch (err) {
			console.error('Failed to duplicate profile:', err);
		}
	}

	async function handleProfileDelete(e: CustomEvent<{ id: string }>) {
		if (confirm(`Delete profile "${$profiles.find(p => p.id === e.detail.id)?.name}"? This cannot be undone.`)) {
			await tabs.deleteProfile(e.detail.id);
		}
	}

	function handleProfileOpenCard(e: CustomEvent<{ type: 'profile' | 'project'; editId?: string }>) {
		dispatch('openProfileCard', { editId: e.detail.editId });
	}

	// Project context menu handlers
	function handleProjectSelect(e: CustomEvent<{ id: string }>) {
		setTabProject(e.detail.id);
	}

	function handleProjectEdit(e: CustomEvent<{ id: string }>) {
		dispatch('openProjectCard', { editId: e.detail.id });
	}

	async function handleProjectDelete(e: CustomEvent<{ id: string }>) {
		if (confirm(`Delete project "${$projects.find(p => p.id === e.detail.id)?.name}"? This cannot be undone.`)) {
			await tabs.deleteProject(e.detail.id);
		}
	}

	function handleProjectOpenCard(e: CustomEvent<{ type: 'profile' | 'project'; editId?: string }>) {
		dispatch('openProjectCard', { editId: e.detail.editId });
	}

	// Computed values
	// Get current profile to access env vars for auto-compaction reserve calculation
	const currentProfile = $derived($profiles.find(p => p.id === tab.profile));

	// Auto-compaction reserve: 13k base + CLAUDE_CODE_MAX_OUTPUT_TOKENS from profile env vars
	const AUTO_COMPACTION_BASE = 13000;
	const maxOutputTokens = $derived(
		parseInt((currentProfile?.config as any)?.env?.CLAUDE_CODE_MAX_OUTPUT_TOKENS || '0', 10) || 0
	);
	const autoCompactionReserve = $derived(AUTO_COMPACTION_BASE + maxOutputTokens);

	// Context usage: use real-time tracked value, or calculate from token counts for resumed sessions
	// Add auto-compaction reserve to show effective context usage
	const baseContextUsed = $derived(tab.contextUsed ?? (tab.totalTokensIn + tab.totalCacheCreationTokens + tab.totalCacheReadTokens));
	const contextUsed = $derived(baseContextUsed + autoCompactionReserve);
	const contextMax = 200000;
	const contextPercent = $derived(Math.min((contextUsed / contextMax) * 100, 100));

	const headerProfilesOrganized = $derived(organizeByGroups($profiles, 'profiles', $groups));
	const hasProfileGroups = $derived(headerProfilesOrganized.groupOrder.length > 0);

	const headerProjectsOrganized = $derived(organizeByGroups($projects, 'projects', $groups));
	const hasProjectGroups = $derived(headerProjectsOrganized.groupOrder.length > 0);
</script>

<div class="flex items-center justify-center gap-2 {compact ? 'flex-wrap' : ''}">
	<!-- Context usage dropdown (always show) -->
	<div class="relative group">
		<button
			class="floating-pill flex items-center gap-1.5 px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground hover:bg-hover-overlay transition-colors"
			title="Context usage: {formatTokenCount(contextUsed)} / {formatTokenCount(contextMax)}"
		>
			<!-- Circular progress indicator -->
			<svg class="w-4 h-4 -rotate-90" viewBox="0 0 20 20">
				<circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" stroke-width="2" opacity="0.2" />
				<circle
					cx="10" cy="10" r="8" fill="none"
					stroke={contextPercent > 80 ? '#ef4444' : contextPercent > 60 ? '#f59e0b' : '#22c55e'}
					stroke-width="2"
					stroke-dasharray={2 * Math.PI * 8}
					stroke-dashoffset={2 * Math.PI * 8 * (1 - contextPercent / 100)}
					stroke-linecap="round"
				/>
			</svg>
			<span>{Math.round(contextPercent)}%</span>
		</button>
		<!-- Token dropdown -->
		<div class="absolute left-0 top-full mt-1 w-52 bg-card border border-border rounded-lg shadow-lg opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-50">
			<div class="py-2 px-3 space-y-2">
				<!-- Context header -->
				<div class="flex items-center justify-between text-xs pb-1 border-b border-border">
					<span class="text-muted-foreground">Context</span>
					<span class="text-foreground font-medium">{formatTokenCount(contextUsed)} / {formatTokenCount(contextMax)}</span>
				</div>
				<div class="flex items-center justify-between text-xs">
					<span class="flex items-center gap-1.5 text-muted-foreground">
						<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16V4m0 0L3 8m4-4l4 4" />
						</svg>
						Input
					</span>
					<span class="text-foreground font-medium">{formatTokenCount(tab.totalTokensIn)}</span>
				</div>
				<div class="flex items-center justify-between text-xs">
					<span class="flex items-center gap-1.5 text-muted-foreground">
						<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 8v12m0 0l4-4m-4 4l-4-4" />
						</svg>
						Output
					</span>
					<span class="text-foreground font-medium">{formatTokenCount(tab.totalTokensOut)}</span>
				</div>
				{#if tab.totalCacheCreationTokens > 0}
					<div class="flex items-center justify-between text-xs">
						<span class="flex items-center gap-1.5 text-muted-foreground">
							<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
							</svg>
							Cache Creation
						</span>
						<span class="text-foreground font-medium">{formatTokenCount(tab.totalCacheCreationTokens)}</span>
					</div>
				{/if}
				{#if tab.totalCacheReadTokens > 0}
					<div class="flex items-center justify-between text-xs">
						<span class="flex items-center gap-1.5 text-blue-400">
							<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
							</svg>
							Cache Read
						</span>
						<span class="text-blue-400 font-medium">{formatTokenCount(tab.totalCacheReadTokens)}</span>
					</div>
				{/if}
			</div>
		</div>
	</div>

	<!-- Profile Selector -->
	{#if $apiUser?.profile_id}
		<!-- API user with locked profile - show as locked indicator -->
		<div class="floating-pill flex items-center gap-1.5 px-3 py-1.5 text-sm text-foreground">
			<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
			</svg>
			<span class="hidden sm:inline max-w-[120px] truncate">{$profiles.find((p) => p.id === $apiUser.profile_id)?.name || $apiUser.profile_id}</span>
			<span title="Profile locked by API key">
				<svg class="w-3 h-3 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
				</svg>
			</span>
		</div>
	{:else}
		<div class="relative group">
			<button
				class="floating-pill flex items-center gap-1.5 px-3 py-1.5 text-sm text-foreground hover:bg-hover-overlay transition-colors"
			>
				<svg class="w-4 h-4 {tab.profile ? 'text-muted-foreground' : 'text-amber-500'}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
				</svg>
				<span class="hidden sm:inline max-w-[120px] truncate {!tab.profile ? 'text-amber-500' : ''}">{$profiles.find((p) => p.id === tab.profile)?.name || 'Select Profile'}</span>
				<svg class="w-3 h-3 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
				</svg>
			</button>
			<!-- Profile dropdown - stays visible when context menu is open -->
			<div class="absolute left-0 top-full mt-1 w-56 bg-card border border-border rounded-lg shadow-lg transition-all z-50 max-h-80 overflow-y-auto {profileContextMenu.show ? 'opacity-100 visible' : 'opacity-0 invisible group-hover:opacity-100 group-hover:visible'}">
				<div class="py-1">
					{#if $profiles.length === 0}
						<p class="px-3 py-2 text-sm text-muted-foreground">No profiles yet</p>
					{:else}
						<!-- Grouped profiles -->
						{#each headerProfilesOrganized.groupOrder as groupItem}
							{@const groupProfiles = headerProfilesOrganized.grouped.get(groupItem.name) || []}
							{#if groupProfiles.length > 0}
								<div class="py-1">
									<button
										class="w-full flex items-center gap-2 px-3 py-1 text-xs font-medium text-muted-foreground hover:text-foreground uppercase tracking-wide bg-muted/30 transition-colors"
										onclick={() => groups.toggleGroupCollapsed('profiles', groupItem.name)}
									>
										<svg class="w-3 h-3 transition-transform {groupItem.collapsed ? '-rotate-90' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
										</svg>
										<span>{groupItem.name}</span>
										<span class="text-muted-foreground/60 ml-auto">({groupProfiles.length})</span>
									</button>
									{#if !groupItem.collapsed}
										{#each groupProfiles as profile}
											<button
												onclick={() => setTabProfile(profile.id)}
												oncontextmenu={(e) => handleProfileContextMenu(e, profile)}
												class="w-full px-3 py-2 text-left text-sm hover:bg-accent transition-colors {tab.profile === profile.id ? 'text-primary bg-accent/50' : 'text-foreground'}"
												title="Right-click for options"
											>
												{profile.name}
											</button>
										{/each}
									{/if}
								</div>
							{/if}
						{/each}
						<!-- Ungrouped profiles -->
						{#if headerProfilesOrganized.ungrouped.length > 0}
							{#if hasProfileGroups}
								<div class="py-1">
									<div class="px-3 py-1 text-xs font-medium text-muted-foreground uppercase tracking-wide bg-muted/30">
										Other
									</div>
								</div>
							{/if}
							{#each headerProfilesOrganized.ungrouped as profile}
								<button
									onclick={() => setTabProfile(profile.id)}
									oncontextmenu={(e) => handleProfileContextMenu(e, profile)}
									class="w-full px-3 py-2 text-left text-sm hover:bg-accent transition-colors {tab.profile === profile.id ? 'text-primary bg-accent/50' : 'text-foreground'}"
									title="Right-click for options"
								>
									{profile.name}
								</button>
							{/each}
						{/if}
					{/if}
				</div>
			</div>
		</div>
	{/if}

	<!-- Project Selector (locked when session exists or for API users) -->
	{#if $apiUser?.project_id}
		<!-- API user with locked project - show as locked indicator -->
		<div class="floating-pill flex items-center gap-1.5 px-3 py-1.5 text-sm text-foreground">
			<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
			</svg>
			<span class="hidden sm:inline max-w-[120px] truncate">{$projects.find((p) => p.id === $apiUser.project_id)?.name || $apiUser.project_id}</span>
			<span title="Project locked by API key">
				<svg class="w-3 h-3 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
				</svg>
			</span>
		</div>
	{:else}
		<div class="relative group">
			<button
				class="floating-pill flex items-center gap-1.5 px-3 py-1.5 text-sm text-foreground {tab.sessionId ? 'cursor-default opacity-75' : 'hover:bg-hover-overlay'} transition-colors"
				title={tab.sessionId ? 'Project is locked for this chat' : 'Select project'}
			>
				<svg class="w-4 h-4 {tab.project ? 'text-muted-foreground' : 'text-amber-500'}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
				</svg>
				<span class="hidden sm:inline max-w-[120px] truncate {!tab.project ? 'text-amber-500' : ''}">{$projects.find((p) => p.id === tab.project)?.name || 'Select Project'}</span>
				{#if tab.sessionId}
					<!-- Lock icon when session exists -->
					<svg class="w-3 h-3 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
					</svg>
				{:else}
					<svg class="w-3 h-3 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				{/if}
			</button>
			<!-- Project dropdown (only show when no session) - stays visible when context menu is open -->
			{#if !tab.sessionId}
				<div class="absolute left-0 top-full mt-1 w-56 bg-card border border-border rounded-lg shadow-lg transition-all z-50 max-h-80 overflow-y-auto {projectContextMenu.show ? 'opacity-100 visible' : 'opacity-0 invisible group-hover:opacity-100 group-hover:visible'}">
					<div class="py-1">
						{#if $projects.length === 0}
							<p class="px-3 py-2 text-sm text-muted-foreground">No projects yet</p>
						{:else}
							<!-- Grouped projects -->
							{#each headerProjectsOrganized.groupOrder as groupItem}
								{@const groupProjects = headerProjectsOrganized.grouped.get(groupItem.name) || []}
								{#if groupProjects.length > 0}
									<div class="py-1">
										<button
											class="w-full flex items-center gap-2 px-3 py-1 text-xs font-medium text-muted-foreground hover:text-foreground uppercase tracking-wide bg-muted/30 transition-colors"
											onclick={() => groups.toggleGroupCollapsed('projects', groupItem.name)}
										>
											<svg class="w-3 h-3 transition-transform {groupItem.collapsed ? '-rotate-90' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
											</svg>
											<span>{groupItem.name}</span>
											<span class="text-muted-foreground/60 ml-auto">({groupProjects.length})</span>
										</button>
										{#if !groupItem.collapsed}
											{#each groupProjects as project}
												<button
													onclick={() => setTabProject(project.id)}
													oncontextmenu={(e) => handleProjectContextMenu(e, project)}
													class="w-full px-3 py-2 text-left text-sm hover:bg-accent transition-colors {tab.project === project.id ? 'text-primary bg-accent/50' : 'text-foreground'}"
													title="Right-click for options"
												>
													{project.name}
												</button>
											{/each}
										{/if}
									</div>
								{/if}
							{/each}
							<!-- Ungrouped projects -->
							{#if headerProjectsOrganized.ungrouped.length > 0}
								{#if hasProjectGroups}
									<div class="py-1">
										<div class="px-3 py-1 text-xs font-medium text-muted-foreground uppercase tracking-wide bg-muted/30">
											Other
										</div>
									</div>
								{/if}
								{#each headerProjectsOrganized.ungrouped as project}
									<button
										onclick={() => setTabProject(project.id)}
										oncontextmenu={(e) => handleProjectContextMenu(e, project)}
										class="w-full px-3 py-2 text-left text-sm hover:bg-accent transition-colors {tab.project === project.id ? 'text-primary bg-accent/50' : 'text-foreground'}"
										title="Right-click for options"
									>
										{project.name}
									</button>
								{/each}
							{/if}
						{/if}
					</div>
				</div>
			{/if}
		</div>
	{/if}
</div>

<!-- Profile Context Menu -->
{#if profileContextMenu.show && profileContextMenu.profile}
	<DropdownContextMenu
		show={profileContextMenu.show}
		x={profileContextMenu.x}
		y={profileContextMenu.y}
		itemId={profileContextMenu.profile.id}
		itemName={profileContextMenu.profile.name}
		itemType="profile"
		isSelected={tab.profile === profileContextMenu.profile.id}
		isBuiltin={profileContextMenu.profile.is_builtin}
		currentGroup={$groups.profiles.assignments[profileContextMenu.profile.id]}
		onClose={closeProfileContextMenu}
		on:select={handleProfileSelect}
		on:edit={handleProfileEdit}
		on:duplicate={handleProfileDuplicate}
		on:delete={handleProfileDelete}
		on:openCard={handleProfileOpenCard}
	/>
{/if}

<!-- Project Context Menu -->
{#if projectContextMenu.show && projectContextMenu.project}
	<DropdownContextMenu
		show={projectContextMenu.show}
		x={projectContextMenu.x}
		y={projectContextMenu.y}
		itemId={projectContextMenu.project.id}
		itemName={projectContextMenu.project.name}
		itemType="project"
		isSelected={tab.project === projectContextMenu.project.id}
		isBuiltin={false}
		currentGroup={$groups.projects.assignments[projectContextMenu.project.id]}
		onClose={closeProjectContextMenu}
		on:select={handleProjectSelect}
		on:edit={handleProjectEdit}
		on:delete={handleProjectDelete}
		on:openCard={handleProjectOpenCard}
	/>
{/if}
