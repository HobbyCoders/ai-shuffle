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

	.dropdown-item .check {
		color: var(--primary);
		font-weight: 600;
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
		background: color-mix(in oklch, var(--card) 90%, var(--foreground));
		border-color: color-mix(in oklch, var(--border) 80%, var(--foreground));
		transform: translateX(3px);
	}

	.list-item:focus-visible {
		outline: 2px solid var(--primary);
		outline-offset: 2px;
	}

	/* Left accent bar - purple tint for API users */
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
		background: var(--accent);
	}

	.list-item.is-streaming .accent-bar {
		background: var(--primary);
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
		background: color-mix(in oklch, var(--muted) 50%, transparent);
		border: 1px solid var(--border-subtle);
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

	/* Title */
	.item-title {
		font-size: 0.875rem;
		font-weight: 450;
		color: var(--foreground);
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

	.clear-filter:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
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
