<script lang="ts">
	/**
	 * ApiUserSessionsList - Admin view for browsing API user chat sessions
	 *
	 * Displays API user sessions with filtering by user.
	 * Only visible to admin users.
	 *
	 * Features:
	 * - Dropdown filter to select specific API user or all
	 * - Session list with API user indicator
	 * - Same styling as RecentSessionsList
	 */

	import { MessageSquare, Zap, Users, ChevronDown, User } from 'lucide-svelte';
	import type { Session } from '$lib/api/client';
	import type { ApiUser } from '$lib/stores/tabs';

	// Extended session interface for API user sessions (includes extra fields from backend)
	interface ApiUserSession extends Session {
		api_user_id?: string;
		message_count?: number;
	}

	interface Props {
		sessions: ApiUserSession[];
		apiUsers: ApiUser[];
		selectedApiUserId: string | null;
		onApiUserChange: (apiUserId: string | null) => void;
		onOpenThread: (sessionId: string) => void;
		isSessionStreaming: (sessionId: string) => boolean;
		formatRelativeTime: (dateStr: string) => string;
	}

	let {
		sessions,
		apiUsers,
		selectedApiUserId,
		onApiUserChange,
		onOpenThread,
		isSessionStreaming,
		formatRelativeTime
	}: Props = $props();

	// Dropdown state
	let dropdownOpen = $state(false);
	let dropdownRef = $state<HTMLDivElement | null>(null);

	// Get selected user name for display
	const selectedUserName = $derived(
		selectedApiUserId
			? apiUsers.find((u) => u.id === selectedApiUserId)?.name ?? 'Unknown User'
			: 'All API Users'
	);

	// Get API user name for a session
	function getApiUserName(session: ApiUserSession): string {
		if (!session.api_user_id) return 'Unknown';
		const user = apiUsers.find((u) => u.id === session.api_user_id);
		return user?.name ?? user?.username ?? session.api_user_id;
	}

	function handleKeyDown(e: KeyboardEvent, sessionId: string) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			onOpenThread(sessionId);
		}
	}

	function handleDropdownKeyDown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			dropdownOpen = false;
		}
	}

	function selectApiUser(userId: string | null) {
		onApiUserChange(userId);
		dropdownOpen = false;
	}

	// Close dropdown when clicking outside
	function handleClickOutside(e: MouseEvent) {
		if (dropdownRef && !dropdownRef.contains(e.target as Node)) {
			dropdownOpen = false;
		}
	}
</script>

<svelte:window onclick={handleClickOutside} />

<div class="api-sessions-container">
	<!-- Filter bar -->
	<div class="filter-bar">
		<div class="filter-dropdown-wrapper" bind:this={dropdownRef}>
			<button
				class="filter-dropdown"
				class:open={dropdownOpen}
				onclick={() => (dropdownOpen = !dropdownOpen)}
				onkeydown={handleDropdownKeyDown}
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
						{#if !selectedApiUserId}
							<span class="check">✓</span>
						{/if}
					</button>

					{#if apiUsers.length > 0}
						<div class="dropdown-divider"></div>
					{/if}

					{#each apiUsers as user (user.id)}
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
							{#if selectedApiUserId === user.id}
								<span class="check">✓</span>
							{/if}
						</button>
					{/each}

					{#if apiUsers.length === 0}
						<div class="dropdown-empty">No API users found</div>
					{/if}
				</div>
			{/if}
		</div>

		<span class="session-count">{sessions.length} session{sessions.length !== 1 ? 's' : ''}</span>
	</div>

	<!-- Session list -->
	<div class="recent-list">
		{#each sessions as session, index (session.id)}
			<button
				class="list-item"
				class:is-streaming={isSessionStreaming(session.id)}
				onclick={() => onOpenThread(session.id)}
				onkeydown={(e) => handleKeyDown(e, session.id)}
				style="animation-delay: {index * 25}ms"
			>
				<!-- Left accent bar -->
				<div class="accent-bar"></div>

				<!-- Message count (left) -->
				<div class="msg-count" class:streaming={isSessionStreaming(session.id)}>
					{#if isSessionStreaming(session.id)}
						<Zap size={12} />
					{:else}
						<MessageSquare size={12} />
					{/if}
					<span>{session.turn_count || session.message_count || 0}</span>
				</div>

				<!-- Content (center) -->
				<div class="item-content">
					<span class="item-title">
						{session.title || 'Untitled conversation'}
					</span>
					<span class="item-user">
						<User size={10} />
						{getApiUserName(session)}
					</span>
				</div>

				<!-- Streaming indicator -->
				{#if isSessionStreaming(session.id)}
					<span class="streaming-badge">
						<span class="streaming-dot"></span>
					</span>
				{/if}

				<!-- Timestamp (right) -->
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
					<button class="clear-filter" onclick={() => onApiUserChange(null)}>
						Show all users
					</button>
				{/if}
			</div>
		{/if}
	</div>
</div>

<style>
	.api-sessions-container {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	/* Filter bar */
	.filter-bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 12px 32px;
		border-bottom: 1px solid var(--border-default, rgba(255, 255, 255, 0.08));
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
		background: var(--bg-card, oklch(0.15 0.01 260));
		border: 1px solid var(--border-default, rgba(255, 255, 255, 0.08));
		border-radius: 8px;
		color: var(--text-primary, #f4f4f5);
		font-size: 0.8125rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.15s ease;
		font-family: inherit;
	}

	.filter-dropdown:hover {
		background: var(--bg-card-hover, oklch(0.17 0.01 260));
		border-color: var(--border-strong, rgba(255, 255, 255, 0.12));
	}

	.filter-dropdown.open {
		border-color: var(--ai-primary, #22d3ee);
		box-shadow: 0 0 0 2px rgba(34, 211, 238, 0.15);
	}

	.filter-dropdown :global(.chevron) {
		transition: transform 0.2s ease;
		color: var(--text-muted, #71717a);
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
		color: var(--text-muted, #71717a);
	}

	/* Dropdown menu */
	.dropdown-menu {
		position: absolute;
		top: calc(100% + 4px);
		left: 0;
		min-width: 220px;
		max-height: 320px;
		overflow-y: auto;
		background: var(--bg-card, oklch(0.13 0.01 260));
		border: 1px solid var(--border-default, rgba(255, 255, 255, 0.1));
		border-radius: 10px;
		box-shadow:
			0 8px 32px rgba(0, 0, 0, 0.4),
			0 0 0 1px rgba(255, 255, 255, 0.05);
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
		border-radius: 6px;
		color: var(--text-primary, #f4f4f5);
		font-size: 0.8125rem;
		cursor: pointer;
		transition: all 0.1s ease;
		text-align: left;
		font-family: inherit;
	}

	.dropdown-item:hover {
		background: var(--hover-overlay, rgba(255, 255, 255, 0.06));
	}

	.dropdown-item.active {
		background: rgba(34, 211, 238, 0.1);
		color: var(--ai-primary, #22d3ee);
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
		color: var(--text-muted, #71717a);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.dropdown-item.active .user-username {
		color: rgba(34, 211, 238, 0.7);
	}

	.dropdown-item .check {
		color: var(--ai-primary, #22d3ee);
		font-weight: 600;
	}

	.dropdown-divider {
		height: 1px;
		background: var(--border-default, rgba(255, 255, 255, 0.08));
		margin: 6px 8px;
	}

	.dropdown-empty {
		padding: 16px;
		text-align: center;
		color: var(--text-muted, #71717a);
		font-size: 0.75rem;
	}

	/* Session list - matches RecentSessionsList */
	.recent-list {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 16px 32px;
		width: 100%;
		max-width: 900px;
		margin: 0 auto;
		overflow-y: auto;
		flex: 1;
	}

	.list-item {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 10px 14px;
		background: var(--bg-card, oklch(0.15 0.01 260));
		border: 1px solid var(--border-default, rgba(255, 255, 255, 0.08));
		border-radius: 8px;
		cursor: pointer;
		transition: all 0.15s ease;
		position: relative;
		overflow: hidden;
		text-align: left;
		width: 100%;
		color: inherit;
		font-family: inherit;

		/* Entry animation */
		opacity: 0;
		transform: translateX(16px);
		animation: listItemEnter 0.25s ease forwards;
	}

	@keyframes listItemEnter {
		to {
			opacity: 1;
			transform: translateX(0);
		}
	}

	.list-item:hover {
		background: var(--bg-card-hover, oklch(0.17 0.01 260));
		border-color: var(--border-strong, rgba(255, 255, 255, 0.12));
		transform: translateX(3px);
	}

	.list-item:focus-visible {
		outline: 2px solid var(--ai-primary, #22d3ee);
		outline-offset: 2px;
	}

	/* Left accent bar - purple tint for API users */
	.accent-bar {
		position: absolute;
		left: 0;
		top: 0;
		bottom: 0;
		width: 3px;
		background: var(--accent-purple, #a855f7);
		opacity: 0.3;
		transition: all 0.15s ease;
	}

	.list-item:hover .accent-bar {
		opacity: 0.7;
		background: var(--accent-purple, #a855f7);
	}

	.list-item.is-streaming .accent-bar {
		background: var(--ai-primary, #22d3ee);
		opacity: 1;
		animation: pulse 2s ease-in-out infinite;
	}

	@keyframes pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.4;
		}
	}

	/* Message count badge (left side) */
	.msg-count {
		display: flex;
		align-items: center;
		gap: 4px;
		min-width: 44px;
		padding: 4px 8px;
		background: rgba(255, 255, 255, 0.04);
		border: 1px solid rgba(255, 255, 255, 0.06);
		border-radius: 6px;
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--text-muted, #71717a);
		flex-shrink: 0;
		transition: all 0.15s ease;
	}

	.list-item:hover .msg-count {
		background: rgba(168, 85, 247, 0.08);
		border-color: rgba(168, 85, 247, 0.15);
		color: var(--text-secondary, #a1a1aa);
	}

	.msg-count.streaming {
		background: rgba(34, 211, 238, 0.12);
		border-color: rgba(34, 211, 238, 0.25);
		color: var(--ai-primary, #22d3ee);
	}

	/* Content column */
	.item-content {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	/* Title */
	.item-title {
		font-size: 0.875rem;
		font-weight: 450;
		color: var(--text-primary, #f4f4f5);
		line-height: 1.35;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	/* API User indicator */
	.item-user {
		display: flex;
		align-items: center;
		gap: 4px;
		font-size: 0.6875rem;
		color: var(--accent-purple, #a855f7);
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
		background: var(--ai-primary, #22d3ee);
		border-radius: 50%;
		animation: blink 1.5s ease-in-out infinite;
		box-shadow: 0 0 6px var(--ai-primary, #22d3ee);
	}

	@keyframes blink {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.3;
		}
	}

	/* Timestamp */
	.item-time {
		font-size: 0.6875rem;
		color: var(--text-dim, #52525b);
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
		color: var(--text-muted, #71717a);
		font-size: 0.9375rem;
	}

	.empty-state :global(svg) {
		opacity: 0.4;
	}

	.clear-filter {
		padding: 8px 16px;
		background: rgba(168, 85, 247, 0.1);
		border: 1px solid rgba(168, 85, 247, 0.2);
		border-radius: 6px;
		color: var(--accent-purple, #a855f7);
		font-size: 0.8125rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.15s ease;
		font-family: inherit;
	}

	.clear-filter:hover {
		background: rgba(168, 85, 247, 0.15);
		border-color: rgba(168, 85, 247, 0.3);
	}

	/* Mobile responsive */
	@media (max-width: 640px) {
		.filter-bar {
			padding: 10px 16px;
		}

		.filter-dropdown {
			padding: 6px 10px;
			font-size: 0.75rem;
		}

		.filter-label {
			max-width: 120px;
		}

		.recent-list {
			padding: 12px 16px;
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
