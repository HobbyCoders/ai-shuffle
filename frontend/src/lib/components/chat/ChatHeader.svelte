<script lang="ts">
	/**
	 * ChatHeader - Context usage indicator, profile selector, and project selector
	 */
	import {
		tabs,
		profiles,
		projects,
		type ChatTab
	} from '$lib/stores/tabs';
	import { isAdmin, apiUser } from '$lib/stores/auth';
	import { groups, organizeByGroups } from '$lib/stores/groups';

	interface Props {
		tab: ChatTab;
		compact?: boolean;
	}

	let { tab, compact = false }: Props = $props();

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

	// Computed values
	const autocompactBuffer = 45000;
	const contextUsed = $derived((tab.contextUsed ?? (tab.totalTokensIn + tab.totalCacheCreationTokens + tab.totalCacheReadTokens)) + autocompactBuffer);
	const contextMax = 200000;
	const contextPercent = $derived(Math.min((contextUsed / contextMax) * 100, 100));

	const headerProfilesOrganized = $derived(organizeByGroups($profiles, 'profiles', $groups));
	const hasProfileGroups = $derived(headerProfilesOrganized.groupOrder.length > 0);

	const headerProjectsOrganized = $derived(organizeByGroups($projects, 'projects', $groups));
	const hasProjectGroups = $derived(headerProjectsOrganized.groupOrder.length > 0);
</script>

<div class="flex items-center gap-2 {compact ? 'flex-wrap' : ''}">
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
			<!-- Profile dropdown -->
			<div class="absolute left-0 top-full mt-1 w-56 bg-card border border-border rounded-lg shadow-lg opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-50 max-h-80 overflow-y-auto">
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
												class="w-full px-3 py-2 text-left text-sm hover:bg-accent transition-colors {tab.profile === profile.id ? 'text-primary bg-accent/50' : 'text-foreground'}"
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
									class="w-full px-3 py-2 text-left text-sm hover:bg-accent transition-colors {tab.profile === profile.id ? 'text-primary bg-accent/50' : 'text-foreground'}"
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
			<!-- Project dropdown (only show when no session) -->
			{#if !tab.sessionId}
				<div class="absolute left-0 top-full mt-1 w-56 bg-card border border-border rounded-lg shadow-lg opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-50 max-h-80 overflow-y-auto">
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
													class="w-full px-3 py-2 text-left text-sm hover:bg-accent transition-colors {tab.project === project.id ? 'text-primary bg-accent/50' : 'text-foreground'}"
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
										class="w-full px-3 py-2 text-left text-sm hover:bg-accent transition-colors {tab.project === project.id ? 'text-primary bg-accent/50' : 'text-foreground'}"
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
