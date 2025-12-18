<script lang="ts">
	/**
	 * Workspace Page
	 *
	 * Universal card system with workspace mode switching.
	 * Each workspace mode (Chats, Files, etc.) has its own cards and sidebar content.
	 */

	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { auth, isAuthenticated, isAdmin, username, apiUser } from '$lib/stores/auth';
	import { workspace, cards, activeMode, WORKSPACE_MODES, type WorkspaceMode } from '$lib/stores/workspace';
	import { tabs, profiles, projects, sessions } from '$lib/stores/tabs';
	import { groupSessionsByDate } from '$lib/utils/dateGroups';
	import { Workspace } from '$lib/components/workspace';

	// Sidebar state
	let sidebarOpen = $state(false);
	let sessionSearchQuery = $state('');
	let collapsedGroups = $state<Set<string>>(new Set(['yesterday', 'week', 'month', 'older']));

	// Get current mode config
	const currentModeConfig = $derived(WORKSPACE_MODES.find(m => m.id === $activeMode) || WORKSPACE_MODES[0]);

	onMount(async () => {
		// Initialize workspace
		await workspace.init();

		// Load data
		await Promise.all([
			tabs.loadProfiles(),
			tabs.loadProjects(),
			tabs.loadSessions()
		]);
	});

	// Open a new item based on current workspace mode
	function openNewItem() {
		const config = currentModeConfig;
		workspace.openCard(config.defaultCardType, {
			title: config.defaultCardType === 'chat' ? 'New Chat' : 'File Browser'
		});
	}

	// Open an existing session as a card
	function openSession(sessionId: string, title: string) {
		workspace.openOrFocusCard('chat', sessionId, { title });
	}

	// Switch to a workspace mode
	function switchToWorkspace(mode: WorkspaceMode) {
		if ($activeMode === mode) {
			// Toggle sidebar if already on this mode
			sidebarOpen = !sidebarOpen;
		} else {
			// Switch mode and open sidebar
			workspace.setMode(mode);
			sidebarOpen = true;
		}
	}

	// Close sidebar
	function closeSidebar() {
		sidebarOpen = false;
	}

	function clearSearch() {
		sessionSearchQuery = '';
	}

	function toggleGroup(groupId: string) {
		const newSet = new Set(collapsedGroups);
		if (newSet.has(groupId)) {
			newSet.delete(groupId);
		} else {
			newSet.add(groupId);
		}
		collapsedGroups = newSet;
	}

	async function handleLogout() {
		await auth.logout();
		goto('/login');
	}

	// Filtered and grouped sessions
	const filteredSessions = $derived(() => {
		let filtered = $sessions;

		if (sessionSearchQuery) {
			const query = sessionSearchQuery.toLowerCase();
			filtered = filtered.filter(s =>
				(s.title?.toLowerCase().includes(query)) ||
				(s.id?.toLowerCase().includes(query))
			);
		}

		return filtered;
	});

	const groupedSessions = $derived(groupSessionsByDate(filteredSessions()));
</script>

<svelte:head>
	<title>AI Hub - Workspace</title>
</svelte:head>

<div class="h-dvh flex bg-background text-foreground">
	<!-- Floating Icon Rail (Desktop) -->
	<nav class="hidden lg:flex fixed left-3 top-3 bottom-3 flex-col w-12 z-50 floating-panel rounded-2xl">
		<div class="flex flex-col items-center pt-3 gap-1">
			<!-- Context-sensitive new item button -->
			<button onclick={openNewItem} class="w-10 h-10 rounded-xl flex items-center justify-center transition-all hover:bg-hover-overlay group" title={currentModeConfig.newItemLabel}>
				<svg class="w-5 h-5 text-primary group-hover:scale-110 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
				</svg>
			</button>
			<div class="w-6 h-px bg-border-subtle my-2"></div>

			<!-- Workspace mode buttons -->
			{#each WORKSPACE_MODES as mode (mode.id)}
				<button
					onclick={() => switchToWorkspace(mode.id)}
					class="relative w-10 h-10 rounded-xl flex items-center justify-center transition-all {$activeMode === mode.id ? 'bg-primary text-primary-foreground shadow-glow' : 'hover:bg-hover-overlay text-muted-foreground hover:text-foreground'}"
					title={mode.name}
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={mode.icon} />
					</svg>
					{#if mode.id === 'chat' && $cards.length > 0}
						<span class="absolute -top-1 -right-1 w-4 h-4 bg-primary text-primary-foreground text-[10px] font-bold rounded-full flex items-center justify-center shadow-glow">{$cards.length}</span>
					{/if}
				</button>
			{/each}
		</div>
		<div class="mt-auto flex flex-col items-center pb-3 gap-1">
			<button onclick={handleLogout} class="w-10 h-10 rounded-full bg-primary/20 flex items-center justify-center hover:bg-primary/30 transition-all" title="Logout ({$apiUser ? $apiUser.name : $username})">
				<span class="text-sm font-medium text-primary">{($apiUser?.name || $username)?.[0]?.toUpperCase() || 'U'}</span>
			</button>
		</div>
	</nav>

	<!-- Floating Expandable Sidebar Panel (Desktop) -->
	{#if sidebarOpen}
		<button class="hidden lg:block fixed inset-0 z-30 bg-black/20 backdrop-blur-[2px]" onclick={closeSidebar} aria-label="Close sidebar"></button>
		<aside class="hidden lg:flex fixed top-3 bottom-3 left-[4.5rem] z-40 w-[340px] floating-panel rounded-2xl flex-col panel-slide-in">
			<!-- Header with workspace name -->
			<div class="p-4 border-b border-border/50 flex items-center justify-between">
				<span class="font-semibold text-foreground">{currentModeConfig.name}</span>
			</div>

			<!-- Panel Content based on active workspace -->
			<div class="flex-1 overflow-y-auto px-3 pb-3 pt-2">
				{#if $activeMode === 'chat'}
					<!-- Search Bar -->
					<div class="pb-3">
						<div class="relative">
							<svg class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
							</svg>
							<input
								type="text"
								bind:value={sessionSearchQuery}
								placeholder="Search conversations..."
								class="w-full pl-9 pr-8 py-2.5 text-sm bg-muted/50 border border-border rounded-xl text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
							/>
							{#if sessionSearchQuery}
								<button onclick={clearSearch} class="absolute right-2 top-1/2 -translate-y-1/2 p-0.5 text-muted-foreground hover:text-foreground transition-colors" title="Clear search">
									<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
									</svg>
								</button>
							{/if}
						</div>
					</div>

					<!-- Open Cards Section -->
					{#if $cards.length > 0}
						<div class="mb-4">
							<div class="flex items-center justify-between px-2 mb-2">
								<span class="text-xs font-medium text-muted-foreground uppercase tracking-wider">Open Cards</span>
								<span class="text-xs text-muted-foreground">{$cards.length}</span>
							</div>
							<div class="space-y-1">
								{#each $cards as card (card.id)}
									<button
										onclick={() => card.state === 'minimized' ? workspace.restoreCard(card.id) : workspace.focusCard(card.id)}
										class="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm transition-colors {card.state === 'minimized' ? 'bg-muted/30 text-muted-foreground' : 'bg-primary/10 text-primary'} hover:bg-primary/20"
									>
										<svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
										</svg>
										<span class="truncate flex-1 text-left">{card.title}</span>
										{#if card.state === 'minimized'}
											<span class="text-xs bg-muted px-1.5 py-0.5 rounded">min</span>
										{/if}
									</button>
								{/each}
							</div>
						</div>
					{/if}

					<!-- Sessions grouped by date -->
					{#each groupedSessions as group (group.key)}
						{#if group.sessions.length > 0}
							<div class="mb-2">
								<button
									onclick={() => toggleGroup(group.key)}
									class="w-full flex items-center justify-between px-2 py-1.5 text-xs font-medium text-muted-foreground uppercase tracking-wider hover:text-foreground transition-colors"
								>
									<span>{group.label}</span>
									<div class="flex items-center gap-2">
										<span class="text-muted-foreground/60">{group.sessions.length}</span>
										<svg class="w-3 h-3 transition-transform {collapsedGroups.has(group.key) ? '-rotate-90' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
										</svg>
									</div>
								</button>
								{#if !collapsedGroups.has(group.key)}
									<div class="space-y-0.5 mt-1">
										{#each group.sessions as session (session.id)}
											<button
												onclick={() => openSession(session.id, session.title || 'Untitled')}
												class="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm text-foreground/80 hover:bg-accent hover:text-foreground transition-colors text-left"
											>
												<span class="truncate">{session.title || 'Untitled'}</span>
											</button>
										{/each}
									</div>
								{/if}
							</div>
						{/if}
					{/each}

				{:else if $activeMode === 'files'}
					<!-- Files workspace sidebar content -->
					<div class="flex flex-col items-center justify-center py-12 text-muted-foreground">
						<svg class="w-12 h-12 mb-4 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
						</svg>
						<p class="text-sm font-medium mb-1">File Browser</p>
						<p class="text-xs text-center px-4">Browse project files and folders. Coming soon!</p>
					</div>
				{/if}
			</div>
		</aside>
	{/if}

	<!-- Main Workspace Area -->
	<main class="flex-1 lg:pl-[4.5rem]">
		<Workspace class="h-full" />
	</main>
</div>

<style>
	/* Floating Panel Styles */
	.floating-panel {
		background: var(--panel-bg);
		backdrop-filter: blur(20px);
		-webkit-backdrop-filter: blur(20px);
		border: 1px solid var(--panel-border);
		box-shadow:
			0 0 0 1px var(--panel-shadow-inset) inset,
			0 8px 32px var(--panel-shadow-outer),
			0 2px 8px var(--panel-shadow-inner);
	}

	/* Glow effect for active buttons */
	.shadow-glow {
		box-shadow:
			0 0 12px oklch(0.72 0.14 180 / 0.4),
			0 0 4px oklch(0.72 0.14 180 / 0.3);
	}

	/* Panel slide-in animation */
	.panel-slide-in {
		animation: panelSlideIn 0.25s cubic-bezier(0.32, 0.72, 0, 1);
	}

	@keyframes panelSlideIn {
		from {
			transform: translateX(-16px);
			opacity: 0;
		}
		to {
			transform: translateX(0);
			opacity: 1;
		}
	}
</style>
