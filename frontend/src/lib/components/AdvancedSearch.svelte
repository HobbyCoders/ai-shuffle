<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { advancedSearch, type AdvancedSearchResult, type Profile, api } from '$lib/api/client';

	interface Props {
		visible: boolean;
		onClose: () => void;
		onSelectSession: (sessionId: string) => void;
	}

	let { visible, onClose, onSelectSession }: Props = $props();

	// Search state
	let searchQuery = $state('');
	let results = $state<AdvancedSearchResult[]>([]);
	let isLoading = $state(false);
	let error = $state<string | null>(null);

	// Filters
	let startDate = $state('');
	let endDate = $state('');
	let profileId = $state('');
	let inCodeOnly = $state(false);
	let useRegex = $state(false);
	let showFilters = $state(false);

	// Profiles for dropdown
	let profiles = $state<Profile[]>([]);

	// Search history (localStorage)
	let searchHistory = $state<string[]>([]);
	let showHistory = $state(false);

	// Refs
	let searchInput: HTMLInputElement;
	let debounceTimer: ReturnType<typeof setTimeout> | null = null;

	// Local storage key
	const HISTORY_KEY = 'ai-hub-search-history';
	const MAX_HISTORY = 10;

	// Load profiles and search history on mount
	onMount(async () => {
		// Load profiles
		try {
			profiles = await api.get<Profile[]>('/profiles');
		} catch (e) {
			console.error('Failed to load profiles:', e);
		}

		// Load search history from localStorage
		try {
			const stored = localStorage.getItem(HISTORY_KEY);
			if (stored) {
				searchHistory = JSON.parse(stored);
			}
		} catch (e) {
			console.error('Failed to load search history:', e);
		}
	});

	// Focus input when modal opens
	$effect(() => {
		if (visible) {
			setTimeout(() => {
				searchInput?.focus();
			}, 50);
		}
	});

	// Debounced search
	function handleSearchInput() {
		if (debounceTimer) {
			clearTimeout(debounceTimer);
		}
		debounceTimer = setTimeout(() => {
			performSearch();
		}, 300);
	}

	async function performSearch() {
		if (!searchQuery.trim()) {
			results = [];
			return;
		}

		isLoading = true;
		error = null;

		try {
			results = await advancedSearch({
				q: searchQuery,
				start_date: startDate || undefined,
				end_date: endDate || undefined,
				profile_id: profileId || undefined,
				in_code_only: inCodeOnly,
				regex: useRegex,
				limit: 50
			});

			// Save to history
			addToHistory(searchQuery);
		} catch (e: unknown) {
			const err = e as { detail?: string };
			error = err.detail || 'Search failed';
			results = [];
		} finally {
			isLoading = false;
		}
	}

	function addToHistory(query: string) {
		if (!query.trim()) return;

		// Remove if already exists
		const filtered = searchHistory.filter(h => h !== query);
		// Add to front
		searchHistory = [query, ...filtered].slice(0, MAX_HISTORY);
		// Save to localStorage
		try {
			localStorage.setItem(HISTORY_KEY, JSON.stringify(searchHistory));
		} catch (e) {
			console.error('Failed to save search history:', e);
		}
	}

	function selectFromHistory(query: string) {
		searchQuery = query;
		showHistory = false;
		performSearch();
	}

	function clearHistory() {
		searchHistory = [];
		try {
			localStorage.removeItem(HISTORY_KEY);
		} catch (e) {
			console.error('Failed to clear search history:', e);
		}
	}

	function handleResultClick(sessionId: string) {
		onSelectSession(sessionId);
		onClose();
	}

	function handleKeyDown(event: KeyboardEvent) {
		if (!visible) return;

		if (event.key === 'Escape') {
			event.preventDefault();
			onClose();
		} else if (event.key === 'Enter' && !event.shiftKey) {
			event.preventDefault();
			performSearch();
		}
	}

	function handleBackdropClick(event: MouseEvent) {
		if (event.target === event.currentTarget) {
			onClose();
		}
	}

	function formatDate(dateStr: string): string {
		const date = new Date(dateStr);
		return date.toLocaleDateString(undefined, {
			year: 'numeric',
			month: 'short',
			day: 'numeric'
		});
	}

	function formatTimeAgo(dateStr: string): string {
		const date = new Date(dateStr);
		const now = new Date();
		const diffMs = now.getTime() - date.getTime();
		const diffMins = Math.floor(diffMs / 60000);
		const diffHours = Math.floor(diffMs / 3600000);
		const diffDays = Math.floor(diffMs / 86400000);

		if (diffMins < 1) return 'just now';
		if (diffMins < 60) return `${diffMins}m ago`;
		if (diffHours < 24) return `${diffHours}h ago`;
		if (diffDays < 7) return `${diffDays}d ago`;
		return formatDate(dateStr);
	}

	function clearFilters() {
		startDate = '';
		endDate = '';
		profileId = '';
		inCodeOnly = false;
		useRegex = false;
	}

	function hasActiveFilters(): boolean {
		return Boolean(startDate || endDate || profileId || inCodeOnly || useRegex);
	}

	onDestroy(() => {
		if (debounceTimer) {
			clearTimeout(debounceTimer);
		}
	});
</script>

<svelte:window on:keydown={handleKeyDown} />

{#if visible}
	<!-- Backdrop with blur -->
	<div
		class="fixed inset-0 max-sm:bottom-[calc(4.5rem+env(safe-area-inset-bottom,0px))] z-50 flex items-start justify-center pt-[10vh] bg-black/60 backdrop-blur-sm"
		onclick={handleBackdropClick}
		role="dialog"
		aria-modal="true"
		aria-label="Advanced search"
	>
		<!-- Modal -->
		<div
			class="w-full max-w-2xl bg-card border border-border rounded-xl overflow-hidden"
			style="box-shadow: var(--shadow-l);"
		>
			<!-- Header -->
			<div class="flex items-center justify-between px-4 py-3 border-b border-border bg-muted/30">
				<h2 class="text-lg font-semibold text-foreground">Advanced Search</h2>
				<button
					type="button"
					onclick={onClose}
					class="p-1.5 text-muted-foreground hover:text-foreground rounded-md hover:bg-muted transition-colors"
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<!-- Search Input -->
			<div class="px-4 py-3 border-b border-border">
				<div class="flex items-center gap-3">
					<svg class="w-5 h-5 text-muted-foreground flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
					</svg>
					<div class="relative flex-1">
						<input
							bind:this={searchInput}
							bind:value={searchQuery}
							oninput={handleSearchInput}
							onfocus={() => showHistory = true}
							onblur={() => setTimeout(() => showHistory = false, 200)}
							type="text"
							placeholder="Search messages, code, content..."
							class="w-full bg-transparent text-foreground placeholder-muted-foreground focus:outline-none text-base"
						/>
						<!-- Search History Dropdown -->
						{#if showHistory && searchHistory.length > 0 && !searchQuery}
							<div class="absolute left-0 right-0 top-full mt-2 bg-card border border-border rounded-lg overflow-hidden z-10">
								<div class="flex items-center justify-between px-3 py-2 border-b border-border">
									<span class="text-xs font-medium text-muted-foreground uppercase">Recent Searches</span>
									<button
										type="button"
										onclick={clearHistory}
										class="text-xs text-muted-foreground hover:text-foreground"
									>
										Clear
									</button>
								</div>
								{#each searchHistory as query}
									<button
										type="button"
										class="w-full px-3 py-2 text-left text-sm text-foreground hover:bg-muted transition-colors flex items-center gap-2"
										onclick={() => selectFromHistory(query)}
									>
										<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
										</svg>
										{query}
									</button>
								{/each}
							</div>
						{/if}
					</div>
					<button
						type="button"
						onclick={() => showFilters = !showFilters}
						class="p-2 text-muted-foreground hover:text-foreground rounded-md hover:bg-muted transition-colors relative"
						title="Toggle filters"
					>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 4a1 1 0 011-1h16a1 1 0 011 1v2.586a1 1 0 01-.293.707l-6.414 6.414a1 1 0 00-.293.707V17l-4 4v-6.586a1 1 0 00-.293-.707L3.293 7.293A1 1 0 013 6.586V4z" />
						</svg>
						{#if hasActiveFilters()}
							<span class="absolute top-1 right-1 w-2 h-2 bg-primary rounded-full"></span>
						{/if}
					</button>
					<button
						type="button"
						onclick={performSearch}
						disabled={!searchQuery.trim() || isLoading}
						class="px-4 py-2 bg-primary text-primary-foreground rounded-md hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors text-sm font-medium"
					>
						{isLoading ? 'Searching...' : 'Search'}
					</button>
				</div>

				<!-- Filters Panel -->
				{#if showFilters}
					<div class="mt-4 pt-4 border-t border-border space-y-4">
						<!-- Date Range -->
						<div class="flex items-center gap-4">
							<label class="text-sm text-muted-foreground w-24">Date Range</label>
							<div class="flex items-center gap-2 flex-1">
								<input
									type="date"
									bind:value={startDate}
									class="flex-1 px-3 py-1.5 text-sm bg-muted border border-border rounded-lg text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
								/>
								<span class="text-muted-foreground">to</span>
								<input
									type="date"
									bind:value={endDate}
									class="flex-1 px-3 py-1.5 text-sm bg-muted border border-border rounded-lg text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
								/>
							</div>
						</div>

						<!-- Profile Filter -->
						<div class="flex items-center gap-4">
							<label class="text-sm text-muted-foreground w-24">Profile</label>
							<select
								bind:value={profileId}
								class="flex-1 px-3 py-1.5 text-sm bg-muted border border-border rounded-lg text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
							>
								<option value="">All profiles</option>
								{#each profiles as profile}
									<option value={profile.id}>{profile.name}</option>
								{/each}
							</select>
						</div>

						<!-- Toggles -->
						<div class="flex items-center gap-6">
							<label class="flex items-center gap-2 cursor-pointer">
								<input
									type="checkbox"
									bind:checked={inCodeOnly}
									class="w-4 h-4 rounded border-border text-primary focus:ring-primary/50"
								/>
								<span class="text-sm text-foreground">Search in code blocks only</span>
							</label>
							<label class="flex items-center gap-2 cursor-pointer">
								<input
									type="checkbox"
									bind:checked={useRegex}
									class="w-4 h-4 rounded border-border text-primary focus:ring-primary/50"
								/>
								<span class="text-sm text-foreground">Use regex</span>
							</label>
							{#if hasActiveFilters()}
								<button
									type="button"
									onclick={clearFilters}
									class="text-sm text-muted-foreground hover:text-foreground ml-auto"
								>
									Clear filters
								</button>
							{/if}
						</div>
					</div>
				{/if}

				<!-- Active Filter Chips -->
				{#if hasActiveFilters() && !showFilters}
					<div class="mt-3 flex flex-wrap gap-2">
						{#if startDate || endDate}
							<span class="inline-flex items-center gap-1 px-2 py-1 bg-muted text-xs text-foreground rounded-md">
								<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
								</svg>
								{startDate || 'Any'} - {endDate || 'Any'}
							</span>
						{/if}
						{#if profileId}
							<span class="inline-flex items-center gap-1 px-2 py-1 bg-muted text-xs text-foreground rounded-md">
								Profile: {profiles.find(p => p.id === profileId)?.name || profileId}
							</span>
						{/if}
						{#if inCodeOnly}
							<span class="inline-flex items-center gap-1 px-2 py-1 bg-muted text-xs text-foreground rounded-md">
								Code only
							</span>
						{/if}
						{#if useRegex}
							<span class="inline-flex items-center gap-1 px-2 py-1 bg-muted text-xs text-foreground rounded-md">
								Regex
							</span>
						{/if}
					</div>
				{/if}
			</div>

			<!-- Results -->
			<div class="max-h-[50vh] overflow-y-auto">
				{#if error}
					<div class="px-4 py-8 text-center text-destructive">
						<p>{error}</p>
					</div>
				{:else if isLoading}
					<div class="px-4 py-8 text-center text-muted-foreground">
						<div class="inline-block animate-spin rounded-full h-6 w-6 border-b-2 border-primary"></div>
						<p class="mt-2">Searching...</p>
					</div>
				{:else if searchQuery && results.length === 0}
					<div class="px-4 py-8 text-center text-muted-foreground">
						<svg class="w-12 h-12 mx-auto mb-3 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.172 16.172a4 4 0 015.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
						</svg>
						<p>No results found for "{searchQuery}"</p>
						<p class="text-sm mt-1">Try different keywords or adjust filters</p>
					</div>
				{:else if results.length > 0}
					<div class="divide-y divide-border">
						{#each results as result}
							<button
								type="button"
								class="w-full px-4 py-3 text-left hover:bg-muted/50 transition-colors"
								onclick={() => handleResultClick(result.session_id)}
							>
								<div class="flex items-start justify-between gap-3">
									<div class="flex-1 min-w-0">
										<h3 class="text-sm font-medium text-foreground truncate">
											{result.session_title || 'Untitled session'}
										</h3>
										<div class="flex items-center gap-2 mt-1 text-xs text-muted-foreground">
											{#if result.profile_name}
												<span class="px-1.5 py-0.5 bg-muted rounded">{result.profile_name}</span>
											{/if}
											<span>{formatTimeAgo(result.updated_at)}</span>
											<span>{result.match_count} match{result.match_count !== 1 ? 'es' : ''}</span>
										</div>
									</div>
								</div>
								<!-- Snippets -->
								{#if result.matches.length > 0}
									<div class="mt-2 space-y-1.5">
										{#each result.matches.slice(0, 3) as match}
											<div class="text-xs text-muted-foreground bg-muted/50 px-2 py-1.5 rounded">
												<span class="text-foreground/70 font-medium mr-1">
													{match.role === 'title' ? 'Title:' : match.role === 'user' ? 'User:' : 'Assistant:'}
												</span>
												<!-- Render snippet with markdown bold as actual bold -->
												{@html match.snippet.replace(/\*\*(.*?)\*\*/g, '<mark class="bg-yellow-500/30 text-foreground px-0.5 rounded">$1</mark>')}
											</div>
										{/each}
										{#if result.matches.length > 3}
											<p class="text-xs text-muted-foreground px-2">
												+{result.matches.length - 3} more matches
											</p>
										{/if}
									</div>
								{/if}
							</button>
						{/each}
					</div>
				{:else}
					<div class="px-4 py-8 text-center text-muted-foreground">
						<svg class="w-12 h-12 mx-auto mb-3 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
						</svg>
						<p>Search across all your conversations</p>
						<p class="text-sm mt-1">Use filters to narrow down results</p>
					</div>
				{/if}
			</div>

			<!-- Footer -->
			<div class="px-4 py-2 border-t border-border bg-muted/30 flex items-center justify-between text-xs text-muted-foreground">
				<div class="flex items-center gap-3">
					<span class="flex items-center gap-1">
						<kbd class="px-1.5 py-0.5 bg-muted rounded">Enter</kbd>
						search
					</span>
					<span class="flex items-center gap-1">
						<kbd class="px-1.5 py-0.5 bg-muted rounded">Esc</kbd>
						close
					</span>
				</div>
				{#if results.length > 0}
					<span>{results.length} result{results.length !== 1 ? 's' : ''}</span>
				{/if}
			</div>
		</div>
	</div>
{/if}
