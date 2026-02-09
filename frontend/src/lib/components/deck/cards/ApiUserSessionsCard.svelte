<script lang="ts">
	/**
	 * ApiUserSessionsCard - Admin view for browsing API user chat sessions
	 *
	 * Displays API user sessions with filtering by user.
	 * Only visible to admin users.
	 */
	import BaseCard from './BaseCard.svelte';
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import type { DeckCard } from './types';
	import { onMount } from 'svelte';
	import {
		MessageSquare,
		Zap,
		Users,
		ChevronDown,
		User,
		RefreshCw,
		Loader2
	} from 'lucide-svelte';
	import { tabs, adminSessions, apiUsers, adminSessionsFilter, allTabs, streamingSessions } from '$lib/stores/tabs';
	import type { Session } from '$lib/api/client';
	import type { ApiUser } from '$lib/stores/tabs';
	import './card-design-system.css';

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		mobile?: boolean;
		embedded?: boolean;
		onSessionClick?: (sessionId: string) => void;
	}

	let {
		card,
		onClose,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		mobile = false,
		embedded = false,
		onSessionClick
	}: Props = $props();

	// Extended session interface for API user sessions
	interface ApiUserSession extends Session {
		api_user_id?: string;
		message_count?: number;
	}

	// State
	let loading = $state(true);
	let refreshing = $state(false);
	let dropdownOpen = $state(false);
	let dropdownRef = $state<HTMLDivElement | null>(null);

	// Derived
	const selectedApiUserId = $derived($adminSessionsFilter);
	const sessions = $derived($adminSessions as ApiUserSession[]);
	const users = $derived($apiUsers);

	const selectedUserName = $derived(
		selectedApiUserId
			? users.find((u) => u.id === selectedApiUserId)?.name ?? 'Unknown User'
			: 'All API Users'
	);

	function getApiUserName(session: ApiUserSession): string {
		if (!session.api_user_id) return 'Unknown';
		const user = users.find((u) => u.id === session.api_user_id);
		return user?.name ?? user?.username ?? session.api_user_id;
	}

	function isStreaming(sessionId: string): boolean {
		const tab = $allTabs.find((t) => t.sessionId === sessionId);
		if (tab?.isStreaming) return true;
		return $streamingSessions.has(sessionId);
	}

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

	function selectApiUser(userId: string | null) {
		tabs.setAdminSessionsFilter(userId);
		dropdownOpen = false;
	}

	function handleClickOutside(e: MouseEvent) {
		if (dropdownRef && !dropdownRef.contains(e.target as Node)) {
			dropdownOpen = false;
		}
	}

	async function handleRefresh() {
		refreshing = true;
		try {
			await Promise.all([
				tabs.loadAdminSessions(selectedApiUserId),
				tabs.loadApiUsers()
			]);
		} finally {
			refreshing = false;
		}
	}

	function handleSessionClick(sessionId: string) {
		if (onSessionClick) {
			onSessionClick(sessionId);
		}
	}

	onMount(async () => {
		try {
			await Promise.all([
				tabs.loadAdminSessions(),
				tabs.loadApiUsers()
			]);
		} finally {
			loading = false;
		}
	});
</script>

<svelte:window onclick={handleClickOutside} />

{#snippet apiSessionsContent()}
	<div class="api-sessions-container">
		{#if loading}
			<div class="loading-state">
				<Loader2 size={24} class="spin" />
				<span>Loading sessions...</span>
			</div>
		{:else}
			<!-- Filter bar -->
			<div class="filter-bar">
				<div class="filter-dropdown-wrapper" bind:this={dropdownRef}>
					<button
						class="filter-dropdown"
						class:open={dropdownOpen}
						onclick={() => (dropdownOpen = !dropdownOpen)}
						onkeydown={(e) => { if (e.key === 'Escape') dropdownOpen = false; }}
						aria-expanded={dropdownOpen}
						aria-haspopup="listbox"
					>
						<Users size={16} />
						<span class="filter-label">{selectedUserName}</span>
						<ChevronDown size={14} class="chevron {dropdownOpen ? 'rotated' : ''}" />
					</button>

					{#if dropdownOpen}
						<div class="dropdown-menu" role="listbox">
							<button
								class="dropdown-item"
								class:active={!selectedApiUserId}
								onclick={() => selectApiUser(null)}
								role="option"
								aria-selected={!selectedApiUserId}
							>
								<Users size={14} />
								<span>All API Users</span>
							</button>

							{#if users.length > 0}
								<div class="dropdown-divider"></div>
							{/if}

							{#each users as user (user.id)}
								<button
									class="dropdown-item"
									class:active={selectedApiUserId === user.id}
									onclick={() => selectApiUser(user.id)}
									role="option"
									aria-selected={selectedApiUserId === user.id}
								>
									<User size={14} />
									<span class="user-info">
										<span class="user-name">{user.name}</span>
										{#if user.username}
											<span class="user-username">@{user.username}</span>
										{/if}
									</span>
								</button>
							{/each}

							{#if users.length === 0}
								<div class="dropdown-empty">No API users found</div>
							{/if}
						</div>
					{/if}
				</div>

				<span class="session-count">{sessions.length} session{sessions.length !== 1 ? 's' : ''}</span>
			</div>

			<!-- Session list -->
			<div class="session-list">
				{#each sessions as session (session.id)}
					<button
						class="list-item"
						class:is-streaming={isStreaming(session.id)}
						onclick={() => handleSessionClick(session.id)}
					>
						<div class="accent-bar"></div>

						<div class="msg-count" class:streaming={isStreaming(session.id)}>
							{#if isStreaming(session.id)}
								<Zap size={12} />
							{:else}
								<MessageSquare size={12} />
							{/if}
							<span>{session.turn_count || session.message_count || 0}</span>
						</div>

						<div class="item-content">
							<span class="item-title">
								{session.title || 'Untitled conversation'}
							</span>
							<span class="item-user">
								<User size={10} />
								{getApiUserName(session)}
							</span>
						</div>

						{#if isStreaming(session.id)}
							<span class="streaming-badge">
								<span class="streaming-dot"></span>
							</span>
						{/if}

						<span class="item-time">
							{formatRelativeTime(session.updated_at)}
						</span>
					</button>
				{/each}

				{#if sessions.length === 0}
					<div class="empty-state">
						<Users size={32} strokeWidth={1} />
						<p>No API user sessions found</p>
						{#if selectedApiUserId}
							<button class="clear-filter" onclick={() => selectApiUser(null)}>
								Show all users
							</button>
						{/if}
					</div>
				{/if}
			</div>
		{/if}
	</div>
{/snippet}

{#if embedded}
	<FeaturePageLayout icon={Users} title="API User Sessions">
		{#snippet headerActions()}
			<button
				class="fp-btn fp-btn--ghost"
				onclick={handleRefresh}
				disabled={refreshing}
				title="Refresh"
			>
				<RefreshCw class="w-4 h-4 {refreshing ? 'spin' : ''}" />
			</button>
		{/snippet}
		{#snippet children()}
			{@render apiSessionsContent()}
		{/snippet}
	</FeaturePageLayout>
{:else if mobile}
	{@render apiSessionsContent()}
{:else}
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		{@render apiSessionsContent()}
	</BaseCard>
{/if}

<style>
	.api-sessions-container {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	/* Loading state */
	.loading-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 12px;
		padding: 60px 20px;
		color: var(--muted-foreground);
		font-size: 0.875rem;
	}

	.loading-state :global(.spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	/* Filter bar */
	.filter-bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 12px 0;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.filter-dropdown-wrapper {
		position: relative;
	}

	.filter-dropdown {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 12px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		color: var(--foreground);
		font-size: 0.8125rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.15s ease;
		font-family: inherit;
	}

	.filter-dropdown:hover {
		background: color-mix(in oklch, var(--card) 90%, var(--foreground));
		border-color: color-mix(in oklch, var(--border) 80%, var(--foreground));
	}

	.filter-dropdown.open {
		border-color: var(--primary);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	.filter-dropdown :global(.chevron) {
		transition: transform 0.2s ease;
		color: var(--muted-foreground);
	}

	.filter-dropdown :global(.chevron.rotated) {
		transform: rotate(180deg);
	}

	.filter-label {
		max-width: 180px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.session-count {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	/* Dropdown menu */
	.dropdown-menu {
		position: absolute;
		top: calc(100% + 4px);
		left: 0;
		min-width: 220px;
		max-height: 320px;
		overflow-y: auto;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		box-shadow: var(--shadow-l);
		z-index: 100;
		padding: 6px;
		animation: dropdownEnter 0.15s ease;
	}

	@keyframes dropdownEnter {
		from {
			opacity: 0;
			transform: translateY(-8px) scale(0.96);
		}
		to {
			opacity: 1;
			transform: translateY(0) scale(1);
		}
	}

	.dropdown-item {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 10px 12px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--foreground);
		font-size: 0.8125rem;
		cursor: pointer;
		transition: all 0.1s ease;
		text-align: left;
		font-family: inherit;
	}

	.dropdown-item:hover {
		background: var(--hover-overlay);
	}

	.dropdown-item.active {
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		color: var(--primary);
	}

	.dropdown-item .user-info {
		display: flex;
		flex-direction: column;
		flex: 1;
		min-width: 0;
	}

	.dropdown-item .user-name {
		font-weight: 500;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.dropdown-item .user-username {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.dropdown-item.active .user-username {
		color: color-mix(in oklch, var(--primary) 70%, transparent);
	}

	.dropdown-divider {
		height: 1px;
		background: var(--border);
		margin: 6px 8px;
	}

	.dropdown-empty {
		padding: 16px;
		text-align: center;
		color: var(--muted-foreground);
		font-size: 0.75rem;
	}

	/* Session list */
	.session-list {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 16px 0;
		width: 100%;
		max-width: 900px;
		overflow-y: auto;
		flex: 1;
	}

	.list-item {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 10px 14px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		cursor: pointer;
		transition: all 0.15s ease;
		position: relative;
		overflow: hidden;
		text-align: left;
		width: 100%;
		color: inherit;
		font-family: inherit;
	}

	.list-item:hover {
		background: color-mix(in oklch, var(--card) 90%, var(--foreground));
		border-color: color-mix(in oklch, var(--border) 80%, var(--foreground));
		transform: translateX(3px);
	}

	.list-item:focus-visible {
		outline: 2px solid var(--primary);
		outline-offset: 2px;
	}

	/* Left accent bar */
	.accent-bar {
		position: absolute;
		left: 0;
		top: 0;
		bottom: 0;
		width: 3px;
		background: var(--accent);
		opacity: 0.3;
		transition: all 0.15s ease;
	}

	.list-item:hover .accent-bar {
		opacity: 0.7;
	}

	.list-item.is-streaming .accent-bar {
		background: var(--primary);
		opacity: 1;
		animation: pulse 2s ease-in-out infinite;
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.4; }
	}

	/* Message count badge */
	.msg-count {
		display: flex;
		align-items: center;
		gap: 4px;
		min-width: 44px;
		padding: 4px 8px;
		background: color-mix(in oklch, var(--muted) 50%, transparent);
		border: 1px solid var(--border-subtle, var(--border));
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--muted-foreground);
		flex-shrink: 0;
		transition: all 0.15s ease;
	}

	.list-item:hover .msg-count {
		background: color-mix(in oklch, var(--accent) 10%, transparent);
		border-color: color-mix(in oklch, var(--accent) 20%, transparent);
		color: var(--foreground);
	}

	.msg-count.streaming {
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		border-color: color-mix(in oklch, var(--primary) 25%, transparent);
		color: var(--primary);
	}

	/* Content column */
	.item-content {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.item-title {
		font-size: 0.875rem;
		font-weight: 450;
		color: var(--foreground);
		line-height: 1.35;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.item-user {
		display: flex;
		align-items: center;
		gap: 4px;
		font-size: 0.6875rem;
		color: var(--accent);
		opacity: 0.8;
	}

	/* Streaming indicator dot */
	.streaming-badge {
		display: flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
	}

	.streaming-dot {
		width: 6px;
		height: 6px;
		background: var(--primary);
		border-radius: 50%;
		animation: blink 1.5s ease-in-out infinite;
		box-shadow: 0 0 6px var(--primary);
	}

	@keyframes blink {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.3; }
	}

	/* Timestamp */
	.item-time {
		font-size: 0.6875rem;
		color: color-mix(in oklch, var(--muted-foreground) 70%, transparent);
		flex-shrink: 0;
		white-space: nowrap;
		min-width: 55px;
		text-align: right;
	}

	/* Empty state */
	.empty-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 12px;
		padding: 60px 20px;
		color: var(--muted-foreground);
		font-size: 0.9375rem;
	}

	.empty-state :global(svg) {
		opacity: 0.4;
	}

	.clear-filter {
		padding: 8px 16px;
		background: color-mix(in oklch, var(--accent) 15%, transparent);
		border: 1px solid color-mix(in oklch, var(--accent) 30%, transparent);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--accent);
		font-size: 0.8125rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.15s ease;
		font-family: inherit;
	}

	.clear-filter:hover {
		background: color-mix(in oklch, var(--accent) 20%, transparent);
		border-color: color-mix(in oklch, var(--accent) 40%, transparent);
	}

	/* Embedded header button styles */
	.fp-btn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 6px 10px;
		border-radius: var(--radius-md, 8px);
		font-size: var(--text-sm, 0.875rem);
		font-weight: 500;
		cursor: pointer;
		border: none;
		transition: all 120ms ease;
	}

	.fp-btn--ghost {
		background: transparent;
		color: var(--text-secondary, var(--muted-foreground));
	}

	.fp-btn--ghost:hover:not(:disabled) {
		background: var(--surface-1, var(--hover-overlay));
		color: var(--text-primary, var(--foreground));
	}

	.fp-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.fp-btn :global(.spin) {
		animation: spin 1s linear infinite;
	}

	/* Mobile responsive */
	@media (max-width: 640px) {
		.filter-bar {
			padding: 10px 0;
		}

		.filter-dropdown {
			padding: 6px 10px;
			font-size: 0.75rem;
		}

		.filter-label {
			max-width: 120px;
		}

		.session-list {
			padding: 12px 0;
			gap: 3px;
		}

		.list-item {
			padding: 8px 10px;
			gap: 8px;
		}

		.msg-count {
			min-width: 38px;
			padding: 3px 6px;
			font-size: 0.6875rem;
		}

		.item-title {
			font-size: 0.8125rem;
		}

		.item-user {
			font-size: 0.625rem;
		}

		.item-time {
			font-size: 0.625rem;
			min-width: 48px;
		}
	}
</style>
