<script lang="ts">
	/**
	 * RecentSessionsList - Compact list view for recent sessions
	 *
	 * Displays sessions in a scannable vertical list with:
	 * - Search bar at top for filtering
	 * - Message count badge (left)
	 * - Session title (center, full width)
	 * - Relative timestamp (right)
	 * - Streaming status indicator
	 * - Infinite scroll for pagination
	 */

	import { MessageSquare, Zap, Search, X, Loader2 } from 'lucide-svelte';
	import type { Session, SessionSearchResult } from '$lib/api/client';
	import { tabs } from '$lib/stores/tabs';

	interface Props {
		sessions: Session[];
		onOpenThread: (sessionId: string) => void;
		isSessionStreaming: (sessionId: string) => boolean;
		formatRelativeTime: (dateStr: string) => string;
	}

	let {
		sessions,
		onOpenThread,
		isSessionStreaming,
		formatRelativeTime
	}: Props = $props();

	// Local search input state
	let searchInput = $state('');
	let searchDebounceTimer: ReturnType<typeof setTimeout> | null = null;

	// Get search state from store
	const searchQuery = $derived($tabs.searchQuery);
	const searchResults = $derived($tabs.searchResults);
	const searchLoading = $derived($tabs.searchLoading);
	const searchLoadingMore = $derived($tabs.searchLoadingMore);
	const searchHasMore = $derived($tabs.searchHasMore);
	const searchTotal = $derived($tabs.searchTotal);

	// Get pagination state from store
	const sessionsLoading = $derived($tabs.sessionsLoading);
	const sessionsLoadingMore = $derived($tabs.sessionsLoadingMore);
	const sessionsHasMore = $derived($tabs.sessionsHasMore);
	const sessionsTotal = $derived($tabs.sessionsTotal);

	// Determine which items to display
	const isSearchMode = $derived(searchQuery.length > 0);
	const displayItems = $derived(isSearchMode ? searchResults : sessions);
	const displayTotal = $derived(isSearchMode ? searchTotal : sessionsTotal);
	const hasMore = $derived(isSearchMode ? searchHasMore : sessionsHasMore);
	const isLoadingMore = $derived(isSearchMode ? searchLoadingMore : sessionsLoadingMore);

	// Reference to the scrollable container for infinite scroll
	let listContainer: HTMLDivElement | null = $state(null);

	// Debounced search handler
	function handleSearchInput(value: string) {
		searchInput = value;

		if (searchDebounceTimer) {
			clearTimeout(searchDebounceTimer);
		}

		searchDebounceTimer = setTimeout(() => {
			if (value.trim()) {
				tabs.searchSessions(value.trim());
			} else {
				tabs.clearSearch();
			}
		}, 300);
	}

	// Clear search
	function clearSearch() {
		searchInput = '';
		tabs.clearSearch();
	}

	// Handle scroll for infinite loading
	function handleScroll(e: Event) {
		const target = e.target as HTMLDivElement;
		const { scrollTop, scrollHeight, clientHeight } = target;

		// Load more when 200px from bottom
		if (scrollHeight - scrollTop - clientHeight < 200) {
			if (hasMore && !isLoadingMore) {
				if (isSearchMode) {
					tabs.loadMoreSearchResults();
				} else {
					tabs.loadMoreSessions();
				}
			}
		}
	}

	function handleKeyDown(e: KeyboardEvent, sessionId: string) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			onOpenThread(sessionId);
		}
	}

	// Check if item is a search result (has match_type)
	function isSearchResult(item: Session | SessionSearchResult): item is SessionSearchResult {
		return 'match_type' in item;
	}
</script>

<div class="recent-container">
	<!-- Search bar -->
	<div class="search-bar">
		<div class="search-input-wrapper">
			<Search size={16} class="search-icon" />
			<input
				type="text"
				placeholder="Search conversations..."
				value={searchInput}
				oninput={(e) => handleSearchInput(e.currentTarget.value)}
				class="search-input"
			/>
			{#if searchInput}
				<button class="clear-search" onclick={clearSearch} aria-label="Clear search">
					<X size={14} />
				</button>
			{/if}
		</div>
		{#if displayTotal > 0}
			<span class="results-count">
				{displayTotal} {displayTotal === 1 ? 'conversation' : 'conversations'}
			</span>
		{/if}
	</div>

	<!-- Sessions list -->
	<div
		class="recent-list"
		bind:this={listContainer}
		onscroll={handleScroll}
	>
		{#if searchLoading}
			<div class="loading-state">
				<Loader2 size={24} class="spinner" />
				<p>Searching...</p>
			</div>
		{:else if sessionsLoading && !isSearchMode}
			<div class="loading-state">
				<Loader2 size={24} class="spinner" />
				<p>Loading conversations...</p>
			</div>
		{:else}
			{#each displayItems as item, index (item.id)}
				<button
					class="list-item"
					class:is-streaming={isSessionStreaming(item.id)}
					onclick={() => onOpenThread(item.id)}
					onkeydown={(e) => handleKeyDown(e, item.id)}
					style="animation-delay: {Math.min(index, 20) * 25}ms"
				>
					<!-- Left accent bar -->
					<div class="accent-bar"></div>

					<!-- Message count (left) -->
					<div class="msg-count" class:streaming={isSessionStreaming(item.id)}>
						{#if isSessionStreaming(item.id)}
							<Zap size={12} />
						{:else}
							<MessageSquare size={12} />
						{/if}
						<span>{item.turn_count || 0}</span>
					</div>

					<!-- Content section -->
					<div class="item-content">
						<!-- Title -->
						<span class="item-title">
							{item.title || 'Untitled conversation'}
						</span>

						<!-- Search match snippet (if in search mode) -->
						{#if isSearchResult(item) && item.match_snippet && item.match_type === 'content'}
							<span class="match-snippet">
								{item.match_snippet}
							</span>
						{/if}
					</div>

					<!-- Streaming indicator -->
					{#if isSessionStreaming(item.id)}
						<span class="streaming-badge">
							<span class="streaming-dot"></span>
						</span>
					{/if}

					<!-- Timestamp (right) -->
					<span class="item-time">
						{formatRelativeTime(item.updated_at)}
					</span>
				</button>
			{/each}

			<!-- Load more indicator -->
			{#if isLoadingMore}
				<div class="loading-more">
					<Loader2 size={16} class="spinner" />
					<span>Loading more...</span>
				</div>
			{/if}

			<!-- Empty state -->
			{#if displayItems.length === 0}
				<div class="empty-state">
					{#if isSearchMode}
						<p>No conversations found for "{searchQuery}"</p>
					{:else}
						<p>No recent conversations</p>
					{/if}
				</div>
			{/if}
		{/if}
	</div>
</div>

<style>
	.recent-container {
		display: flex;
		flex-direction: column;
		height: 100%;
		width: 100%;
		max-width: 900px;
		margin: 0 auto;
	}

	/* Search bar styles */
	.search-bar {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 16px 32px 12px;
		flex-shrink: 0;
	}

	.search-input-wrapper {
		flex: 1;
		position: relative;
		display: flex;
		align-items: center;
	}

	.search-input-wrapper :global(.search-icon) {
		position: absolute;
		left: 12px;
		color: var(--text-muted, #71717a);
		pointer-events: none;
	}

	.search-input {
		width: 100%;
		padding: 10px 36px 10px 40px;
		background: var(--bg-card, oklch(0.15 0.01 260));
		border: 1px solid var(--border-default, rgba(255, 255, 255, 0.08));
		border-radius: var(--radius, 0.75rem);
		font-size: 0.875rem;
		color: var(--text-primary, #f4f4f5);
		outline: none;
		transition: all 0.15s ease;
	}

	.search-input::placeholder {
		color: var(--text-muted, #71717a);
	}

	.search-input:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	.clear-search {
		position: absolute;
		right: 10px;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 4px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		color: var(--text-muted, #71717a);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.clear-search:hover {
		background: rgba(255, 255, 255, 0.1);
		color: var(--text-primary, #f4f4f5);
	}

	.results-count {
		font-size: 0.75rem;
		color: var(--text-muted, #71717a);
		white-space: nowrap;
	}

	/* List styles */
	.recent-list {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 4px 32px 16px;
		width: 100%;
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
		background: var(--bg-card-hover, oklch(0.17 0.01 260));
		border-color: var(--border-strong, rgba(255, 255, 255, 0.12));
		transform: translateX(3px);
	}

	.list-item:focus-visible {
		outline: 2px solid var(--primary);
		outline-offset: 2px;
	}

	/* Left accent bar - subtle monochrome */
	.accent-bar {
		position: absolute;
		left: 0;
		top: 0;
		bottom: 0;
		width: 3px;
		background: var(--text-dim, #52525b);
		opacity: 0.3;
		transition: all 0.15s ease;
	}

	.list-item:hover .accent-bar {
		opacity: 0.7;
		background: var(--primary);
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

	/* Message count badge (left side) */
	.msg-count {
		display: flex;
		align-items: center;
		gap: 4px;
		min-width: 44px;
		padding: 4px 8px;
		background: rgba(255, 255, 255, 0.04);
		border: 1px solid rgba(255, 255, 255, 0.06);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--text-muted, #71717a);
		flex-shrink: 0;
		transition: all 0.15s ease;
	}

	.list-item:hover .msg-count {
		background: color-mix(in oklch, var(--primary) 8%, transparent);
		border-color: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--text-secondary, #a1a1aa);
	}

	.msg-count.streaming {
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		border-color: color-mix(in oklch, var(--primary) 25%, transparent);
		color: var(--primary);
	}

	/* Content section with title and optional snippet */
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

	/* Search match snippet */
	.match-snippet {
		font-size: 0.75rem;
		color: var(--text-muted, #71717a);
		line-height: 1.3;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
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
		color: var(--text-dim, #52525b);
		flex-shrink: 0;
		white-space: nowrap;
		min-width: 55px;
		text-align: right;
	}

	/* Loading states */
	.loading-state,
	.loading-more {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 24px 20px;
		color: var(--text-muted, #71717a);
		font-size: 0.875rem;
	}

	.loading-more {
		padding: 12px 20px;
	}

	:global(.spinner) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	/* Empty state */
	.empty-state {
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 60px 20px;
		color: var(--text-muted, #71717a);
		font-size: 0.9375rem;
	}

	/* Mobile responsive */
	@media (max-width: 640px) {
		.search-bar {
			padding: 12px 16px 8px;
		}

		.search-input {
			padding: 8px 32px 8px 36px;
			font-size: 0.8125rem;
		}

		.results-count {
			display: none;
		}

		.recent-list {
			padding: 4px 16px 12px;
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

		.match-snippet {
			font-size: 0.6875rem;
		}

		.item-time {
			font-size: 0.625rem;
			min-width: 48px;
		}
	}
</style>
