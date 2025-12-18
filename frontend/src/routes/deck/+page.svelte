<script lang="ts">
	/**
	 * The Deck - Desktop-like AI Workspace (Preview)
	 *
	 * A preview of the new card-based workspace UI.
	 * This is a simplified version to demonstrate the layout.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { isAuthenticated } from '$lib/stores/auth';
	import { tabs, allTabs, sessions } from '$lib/stores/tabs';

	// Deck components
	import { DeckLayout } from '$lib/components/deck';
	import { AgentsView } from '$lib/components/deck/agents';
	import { StudioView } from '$lib/components/deck/studio';

	// Types from deck/types
	import type {
		ActivityMode,
		DeckSession,
		DeckAgent,
		DeckGeneration,
		MinimizedCard,
		RunningProcess
	} from '$lib/components/deck/types';

	// Local state
	let activeMode = $state<ActivityMode>('workspace');
	let contextCollapsed = $state(false);
	let isMobile = $state(false);

	// Auth check on mount
	onMount(async () => {
		if (!$isAuthenticated) {
			await goto('/login');
			return;
		}

		// Initialize tabs store
		await tabs.init();

		// Check mobile
		checkMobile();
		window.addEventListener('resize', checkMobile);
		window.addEventListener('keydown', handleGlobalKeydown);
	});

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			window.removeEventListener('resize', checkMobile);
			window.removeEventListener('keydown', handleGlobalKeydown);
		}
	});

	function checkMobile() {
		isMobile = window.innerWidth < 640;
	}

	// Handle global keyboard shortcuts
	function handleGlobalKeydown(e: KeyboardEvent) {
		// Cmd/Ctrl+K - Open main chat
		if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
			e.preventDefault();
			goto('/');
		}
		// Escape - Go back to main
		if (e.key === 'Escape') {
			goto('/');
		}
	}

	// Handle mode change
	function handleModeChange(mode: ActivityMode) {
		activeMode = mode;
	}

	// Derive sessions for context panel
	const deckSessions = $derived<DeckSession[]>(
		$sessions.slice(0, 20).map(s => ({
			id: s.id,
			title: s.title || 'Untitled',
			lastMessage: s.title || undefined,
			timestamp: new Date(s.updated_at),
			isActive: $allTabs.some(t => t.sessionId === s.id)
		}))
	);

	// Empty arrays for now (agents/studio integration pending)
	const deckAgents: DeckAgent[] = [];
	const deckGenerations: DeckGeneration[] = [];
	const minimizedCards: MinimizedCard[] = [];
	const runningProcesses: RunningProcess[] = [];

	// Badges
	const badges = $derived<import('$lib/components/deck/types').ActivityBadges>({
		workspace: $allTabs.length > 0 ? $allTabs.length : undefined,
		agents: undefined,
		studio: undefined,
		files: undefined
	});

	// Handle session click - go to main chat with that session
	function handleSessionClick(session: DeckSession) {
		// For now, just go to main page
		goto('/');
	}
</script>

{#if !$isAuthenticated}
	<div class="flex items-center justify-center h-screen bg-background">
		<p class="text-muted-foreground">Redirecting to login...</p>
	</div>
{:else}
	<DeckLayout
		{activeMode}
		{badges}
		contextCollapsed={contextCollapsed}
		sessions={deckSessions}
		agents={deckAgents}
		generations={deckGenerations}
		currentSession={null}
		{minimizedCards}
		{runningProcesses}
		onModeChange={handleModeChange}
		onLogoClick={() => goto('/')}
		onSettingsClick={() => goto('/')}
		onContextToggle={() => contextCollapsed = !contextCollapsed}
		onSessionClick={handleSessionClick}
		onAgentClick={() => {}}
		onGenerationClick={() => {}}
		onMinimizedCardClick={() => {}}
		onProcessClick={() => {}}
	>
		{#if activeMode === 'workspace'}
			<!-- Workspace - Empty state with welcome message -->
			<div class="h-full flex items-center justify-center p-8">
				<div class="text-center max-w-lg">
					<div class="w-20 h-20 mx-auto mb-6 rounded-3xl bg-gradient-to-br from-primary/20 to-primary/5 flex items-center justify-center">
						<svg class="w-10 h-10 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M4 5a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1V5zM4 15a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1v-4zM14 5a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1V5zM14 15a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z" />
						</svg>
					</div>
					<h1 class="text-2xl font-semibold text-foreground mb-3">The Deck</h1>
					<p class="text-muted-foreground mb-6">
						A desktop-like workspace with draggable, resizable cards for chat, agents, and media generation.
					</p>
					<div class="flex flex-col sm:flex-row gap-3 justify-center">
						<a
							href="/"
							class="inline-flex items-center justify-center gap-2 px-6 py-3 bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-colors font-medium"
						>
							<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
							</svg>
							Open Chat
						</a>
					</div>
					<p class="text-xs text-muted-foreground/70 mt-8">
						This is a preview of The Deck architecture. Full functionality coming soon.
					</p>
				</div>
			</div>
		{:else if activeMode === 'agents'}
			<AgentsView />
		{:else if activeMode === 'studio'}
			<StudioView />
		{:else}
			<!-- Files mode placeholder -->
			<div class="flex items-center justify-center h-full">
				<div class="text-center">
					<div class="w-16 h-16 mx-auto mb-4 rounded-2xl bg-muted/50 flex items-center justify-center">
						<svg class="w-8 h-8 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
						</svg>
					</div>
					<p class="text-lg text-muted-foreground mb-2">File Browser</p>
					<p class="text-sm text-muted-foreground/70">Coming soon</p>
				</div>
			</div>
		{/if}
	</DeckLayout>
{/if}
