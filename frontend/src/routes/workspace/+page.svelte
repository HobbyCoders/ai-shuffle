<script lang="ts">
	/**
	 * Workspace Demo Page
	 *
	 * This is a standalone page to test and develop the universal card system
	 * before integrating it into the main chat page.
	 */

	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { auth, isAuthenticated } from '$lib/stores/auth';
	import { workspace, cards, activeMode } from '$lib/stores/workspace';
	import { tabs, profiles, projects, sessions } from '$lib/stores/tabs';
	import { Workspace, ModeRail } from '$lib/components/workspace';

	// Sidebar state
	let sidebarOpen = $state(true);

	onMount(async () => {
		// Check auth
		await auth.init();
		if (!$isAuthenticated) {
			goto('/login');
			return;
		}

		// Initialize workspace
		await workspace.init();

		// Load data
		await Promise.all([
			tabs.loadProfiles(),
			tabs.loadProjects(),
			tabs.loadSessions()
		]);
	});

	// Open a new chat card
	function openNewChat() {
		workspace.openCard('chat', {
			title: 'New Chat'
		});
	}

	// Open an existing session
	function openSession(sessionId: string, title: string) {
		workspace.openOrFocusCard('chat', sessionId, { title });
	}

	// Toggle sidebar
	function toggleSidebar() {
		sidebarOpen = !sidebarOpen;
	}
</script>

<svelte:head>
	<title>AI Hub - Workspace</title>
</svelte:head>

<div class="workspace-page">
	<!-- Sidebar -->
	<aside class="sidebar" class:collapsed={!sidebarOpen}>
		<!-- Toggle button -->
		<button class="sidebar-toggle" onclick={toggleSidebar}>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
				{#if sidebarOpen}
					<path d="M15 18l-6-6 6-6" />
				{:else}
					<path d="M9 18l6-6-6-6" />
				{/if}
			</svg>
		</button>

		{#if sidebarOpen}
			<!-- Mode Rail -->
			<ModeRail />

			<!-- Content based on mode -->
			{#if $activeMode === 'chat'}
				<div class="sidebar-content">
					<!-- New Chat button -->
					<button class="new-chat-btn" onclick={openNewChat}>
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<path d="M12 4v16m8-8H4" />
						</svg>
						<span>New Chat</span>
					</button>

					<!-- Open Cards -->
					{#if $cards.length > 0}
						<div class="section">
							<div class="section-header">Open Cards</div>
							<div class="session-list">
								{#each $cards as card (card.id)}
									<button
										class="session-item"
										class:active={card.state !== 'minimized'}
										onclick={() => workspace.focusCard(card.id)}
									>
										<span class="session-title">{card.title}</span>
										{#if card.state === 'minimized'}
											<span class="badge">minimized</span>
										{/if}
									</button>
								{/each}
							</div>
						</div>
					{/if}

					<!-- Recent Sessions -->
					<div class="section">
						<div class="section-header">Recent Sessions</div>
						<div class="session-list">
							{#each $sessions.slice(0, 10) as session (session.id)}
								<button
									class="session-item"
									onclick={() => openSession(session.id, session.title || 'Untitled')}
								>
									<span class="session-title">{session.title || 'Untitled'}</span>
								</button>
							{/each}
							{#if $sessions.length === 0}
								<p class="empty-text">No sessions yet</p>
							{/if}
						</div>
					</div>
				</div>
			{/if}
		{/if}
	</aside>

	<!-- Main Workspace Area -->
	<main class="main-area">
		<Workspace />
	</main>
</div>

<style>
	.workspace-page {
		display: flex;
		height: 100dvh;
		background: #0a0a0a;
		color: white;
	}

	.sidebar {
		position: relative;
		width: 280px;
		background: #141414;
		border-right: 1px solid rgba(255, 255, 255, 0.1);
		display: flex;
		flex-direction: column;
		transition: width 0.2s ease;
	}

	.sidebar.collapsed {
		width: 48px;
	}

	.sidebar-toggle {
		position: absolute;
		right: -12px;
		top: 50%;
		transform: translateY(-50%);
		width: 24px;
		height: 24px;
		background: #2a2a2a;
		border: 1px solid rgba(255, 255, 255, 0.1);
		border-radius: 50%;
		display: flex;
		align-items: center;
		justify-content: center;
		cursor: pointer;
		z-index: 10;
		color: rgba(255, 255, 255, 0.6);
		transition: all 0.15s ease;
	}

	.sidebar-toggle:hover {
		background: #3a3a3a;
		color: white;
	}

	.sidebar-toggle svg {
		width: 14px;
		height: 14px;
	}

	.sidebar-content {
		flex: 1;
		overflow-y: auto;
		padding: 12px;
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	.new-chat-btn {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 10px 12px;
		background: #fb923c;
		border: none;
		border-radius: 8px;
		color: #1a1a1a;
		font-size: 0.875rem;
		font-weight: 600;
		cursor: pointer;
		transition: background 0.15s ease;
	}

	.new-chat-btn:hover {
		background: #f97316;
	}

	.new-chat-btn svg {
		width: 18px;
		height: 18px;
	}

	.section {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.section-header {
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.1em;
		color: rgba(255, 255, 255, 0.4);
		padding-left: 4px;
	}

	.session-list {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.session-item {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 8px 10px;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: rgba(255, 255, 255, 0.7);
		font-size: 0.8125rem;
		text-align: left;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.session-item:hover {
		background: rgba(255, 255, 255, 0.05);
		color: white;
	}

	.session-item.active {
		background: rgba(255, 165, 0, 0.1);
		color: #fb923c;
	}

	.session-title {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.badge {
		font-size: 0.625rem;
		padding: 2px 6px;
		background: rgba(255, 255, 255, 0.1);
		border-radius: 4px;
		color: rgba(255, 255, 255, 0.5);
	}

	.empty-text {
		font-size: 0.8125rem;
		color: rgba(255, 255, 255, 0.4);
		padding: 8px 10px;
	}

	.main-area {
		flex: 1;
		display: flex;
		overflow: hidden;
	}
</style>
