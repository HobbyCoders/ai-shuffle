<script lang="ts">
	import {
		Image as ImageIcon,
		Film,
		Volume2,
		RefreshCw,
		Trash2,
		Clock,
		CheckCircle2,
		XCircle,
		Loader2,
		Search,
		Filter,
		ChevronDown
	} from 'lucide-svelte';
	import type { DeckGeneration } from '../types';
	import { recentGenerations, studio } from '$lib/stores/studio';
	import type { DeckGeneration as StoreDeckGeneration } from '$lib/stores/studio';

	// Props
	interface Props {
		onSelect?: (generation: DeckGeneration) => void;
		onRegenerate?: (generation: DeckGeneration) => void;
		onDelete?: (generation: DeckGeneration) => void;
	}

	let { onSelect, onRegenerate, onDelete }: Props = $props();

	// State
	let hoveredId: string | null = $state(null);
	let searchQuery = $state('');
	let activeFilter: 'all' | 'image' | 'video' | 'audio' = $state('all');
	let groupByDate = $state(true);

	// Map store generation to component's expected format
	function mapStoreGeneration(gen: StoreDeckGeneration): DeckGeneration | null {
		// Skip invalid generations (missing required fields)
		if (!gen || !gen.id || !gen.type || !gen.prompt) {
			console.warn('[GenerationHistory] Skipping invalid generation:', gen);
			return null;
		}

		// Map status: store uses 'completed'/'failed', component uses 'complete'/'error'
		let status: DeckGeneration['status'];
		switch (gen.status) {
			case 'completed':
				status = 'complete';
				break;
			case 'failed':
				status = 'error';
				break;
			default:
				status = gen.status;
		}

		// Map type: store uses 'tts'/'stt', component expects 'audio'
		let type: DeckGeneration['type'] = gen.type as DeckGeneration['type'];
		if (gen.type === 'tts' || gen.type === 'stt') {
			type = 'audio';
		}

		return {
			id: gen.id,
			type,
			prompt: gen.prompt,
			status,
			progress: gen.progress ?? 0,
			thumbnailUrl: gen.result?.url,
			resultUrl: gen.result?.url,
			provider: gen.provider,
			model: gen.model
		};
	}

	// Derived history from store - filter out null/invalid generations
	let history = $derived(
		$recentGenerations
			.map(mapStoreGeneration)
			.filter((gen): gen is DeckGeneration => gen !== null)
	);

	// Filtered history based on search and filter
	let filteredHistory = $derived.by(() => {
		let filtered = history;

		// Apply type filter
		if (activeFilter !== 'all') {
			filtered = filtered.filter((gen) => gen.type === activeFilter);
		}

		// Apply search filter
		if (searchQuery.trim()) {
			const query = searchQuery.toLowerCase();
			filtered = filtered.filter((gen) => gen.prompt.toLowerCase().includes(query));
		}

		return filtered;
	});

	// Group generations by date
	type GroupedGenerations = {
		label: string;
		generations: DeckGeneration[];
	};

	let groupedHistory = $derived.by(() => {
		if (!groupByDate) {
			return [{ label: 'All', generations: filteredHistory }];
		}

		const groups: GroupedGenerations[] = [];
		const now = new Date();
		const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
		const yesterday = new Date(today);
		yesterday.setDate(yesterday.getDate() - 1);
		const thisWeek = new Date(today);
		thisWeek.setDate(thisWeek.getDate() - 7);

		const todayGens: DeckGeneration[] = [];
		const yesterdayGens: DeckGeneration[] = [];
		const thisWeekGens: DeckGeneration[] = [];
		const olderGens: DeckGeneration[] = [];

		filteredHistory.forEach((gen) => {
			// For now, we'll just group them all as "Recent" since we don't have timestamps
			// In a real implementation, you'd use gen.timestamp or similar
			todayGens.push(gen);
		});

		if (todayGens.length > 0) {
			groups.push({ label: 'Today', generations: todayGens });
		}
		if (yesterdayGens.length > 0) {
			groups.push({ label: 'Yesterday', generations: yesterdayGens });
		}
		if (thisWeekGens.length > 0) {
			groups.push({ label: 'This Week', generations: thisWeekGens });
		}
		if (olderGens.length > 0) {
			groups.push({ label: 'Older', generations: olderGens });
		}

		return groups.length > 0 ? groups : [{ label: 'Recent', generations: filteredHistory }];
	});

	// Count by type
	let imageCount = $derived(history.filter((g) => g.type === 'image').length);
	let videoCount = $derived(history.filter((g) => g.type === 'video').length);
	let audioCount = $derived(history.filter((g) => g.type === 'audio').length);

	// Handle delete with store
	function handleDeleteGeneration(generation: DeckGeneration, event: MouseEvent) {
		event.stopPropagation();
		studio.removeGeneration(generation.id);
		onDelete?.(generation);
	}

	// Handlers
	function handleSelect(generation: DeckGeneration) {
		if (generation.status === 'complete') {
			onSelect?.(generation);
		}
	}

	function handleRegenerate(generation: DeckGeneration, event: MouseEvent) {
		event.stopPropagation();
		onRegenerate?.(generation);
	}

	function handleDelete(generation: DeckGeneration, event: MouseEvent) {
		handleDeleteGeneration(generation, event);
	}

	function getStatusIcon(status: DeckGeneration['status']) {
		switch (status) {
			case 'pending':
				return Clock;
			case 'generating':
				return Loader2;
			case 'complete':
				return CheckCircle2;
			case 'error':
				return XCircle;
			default:
				return Clock;
		}
	}

	function getStatusColor(status: DeckGeneration['status']): string {
		switch (status) {
			case 'pending':
				return 'text-muted-foreground';
			case 'generating':
				return 'text-primary';
			case 'complete':
				return 'text-green-500';
			case 'error':
				return 'text-destructive';
			default:
				return 'text-muted-foreground';
		}
	}

	function getTypeIcon(type: DeckGeneration['type']) {
		switch (type) {
			case 'image':
				return ImageIcon;
			case 'video':
				return Film;
			case 'audio':
				return Volume2;
			default:
				return ImageIcon;
		}
	}

	function truncatePrompt(prompt: string, maxLength: number = 40): string {
		if (prompt.length <= maxLength) return prompt;
		return prompt.substring(0, maxLength) + '...';
	}

	function handleSearchInput(event: Event) {
		const input = event.target as HTMLInputElement;
		searchQuery = input.value;
	}
</script>

<div class="cosmic-history h-full flex flex-col">
	<!-- Header with search and filters -->
	<div class="shrink-0 p-4 space-y-3 border-b border-primary/10">
		<!-- Search bar -->
		<div class="search-container">
			<Search class="search-icon" />
			<input
				type="text"
				value={searchQuery}
				oninput={handleSearchInput}
				placeholder="Search generations..."
				class="search-input"
			/>
			{#if searchQuery}
				<button
					type="button"
					onclick={() => (searchQuery = '')}
					class="search-clear"
					aria-label="Clear search"
				>
					<XCircle class="w-4 h-4" />
				</button>
			{/if}
		</div>

		<!-- Filter chips -->
		<div class="flex items-center gap-2 flex-wrap">
			<button
				type="button"
				onclick={() => (activeFilter = 'all')}
				class="filter-chip {activeFilter === 'all' ? 'filter-chip-active' : ''}"
			>
				All
				<span class="filter-count">{history.length}</span>
			</button>
			<button
				type="button"
				onclick={() => (activeFilter = 'image')}
				class="filter-chip {activeFilter === 'image' ? 'filter-chip-active' : ''}"
			>
				<ImageIcon class="w-3.5 h-3.5" />
				Images
				<span class="filter-count">{imageCount}</span>
			</button>
			<button
				type="button"
				onclick={() => (activeFilter = 'video')}
				class="filter-chip {activeFilter === 'video' ? 'filter-chip-active' : ''}"
			>
				<Film class="w-3.5 h-3.5" />
				Videos
				<span class="filter-count">{videoCount}</span>
			</button>
			<button
				type="button"
				onclick={() => (activeFilter = 'audio')}
				class="filter-chip {activeFilter === 'audio' ? 'filter-chip-active' : ''}"
			>
				<Volume2 class="w-3.5 h-3.5" />
				Audio
				<span class="filter-count">{audioCount}</span>
			</button>
		</div>
	</div>

	<!-- Timeline/Grid -->
	<div class="flex-1 overflow-y-auto custom-scrollbar">
		{#if filteredHistory.length === 0}
			<!-- Empty state -->
			<div class="h-full flex flex-col items-center justify-center p-8 text-center">
				<div class="empty-state-icon mb-4">
					<Clock class="w-8 h-8 text-primary" />
				</div>
				<h3 class="text-sm font-semibold text-foreground mb-2">
					{searchQuery ? 'No results found' : 'No generations yet'}
				</h3>
				<p class="text-xs text-muted-foreground max-w-xs">
					{searchQuery
						? 'Try adjusting your search or filters'
						: 'Start generating content to see your history here'}
				</p>
			</div>
		{:else}
			<div class="p-4 space-y-6">
				{#each groupedHistory as group (group.label)}
					<div class="generation-group">
						<!-- Group header -->
						<div class="group-header">
							<h3 class="group-title">{group.label}</h3>
							<div class="group-count">{group.generations.length}</div>
						</div>

						<!-- Generation grid -->
						<div class="generation-grid">
							{#each group.generations as generation (generation.id)}
								{@const StatusIcon = getStatusIcon(generation.status)}
								{@const TypeIcon = getTypeIcon(generation.type)}
								<div
									role="button"
									tabindex={generation.status === 'complete' ? 0 : -1}
									onclick={() => handleSelect(generation)}
									onkeydown={(e) =>
										(e.key === 'Enter' || e.key === ' ') && handleSelect(generation)}
									onmouseenter={() => (hoveredId = generation.id)}
									onmouseleave={() => (hoveredId = null)}
									class="generation-card {generation.status !== 'complete'
										? 'generation-card-disabled'
										: ''} {hoveredId === generation.id ? 'generation-card-hover' : ''}"
									aria-label="View generation: {truncatePrompt(generation.prompt)}"
									aria-disabled={generation.status !== 'complete'}
								>
									<!-- Thumbnail area -->
									<div class="generation-thumb">
										{#if generation.thumbnailUrl}
											<img
												src={generation.thumbnailUrl}
												alt={generation.prompt}
												class="generation-image"
											/>
										{:else}
											<!-- Placeholder -->
											<div class="generation-placeholder">
												<TypeIcon class="w-8 h-8 text-muted-foreground/50" />
											</div>
										{/if}

										<!-- Type badge -->
										<div class="type-badge">
											<TypeIcon class="w-3 h-3" />
										</div>

										<!-- Status indicator -->
										<div class="status-indicator {getStatusColor(generation.status)}">
											{#if generation.status === 'generating'}
												<StatusIcon class="w-4 h-4 animate-spin" />
											{:else}
												<StatusIcon class="w-4 h-4" />
											{/if}
										</div>

										<!-- Progress bar for generating items -->
										{#if generation.status === 'generating' && generation.progress !== undefined}
											<div class="progress-bar-container">
												<div
													class="progress-bar"
													style="width: {generation.progress}%"
												></div>
											</div>
										{/if}

										<!-- Hover overlay with actions -->
										{#if generation.status === 'complete' && hoveredId === generation.id}
											<div class="hover-overlay">
												<button
													type="button"
													onclick={(e) => handleRegenerate(generation, e)}
													class="action-btn-small"
													aria-label="Re-generate"
													title="Re-generate"
												>
													<RefreshCw class="w-4 h-4" />
												</button>
												<button
													type="button"
													onclick={(e) => handleDelete(generation, e)}
													class="action-btn-small action-btn-danger"
													aria-label="Delete"
													title="Delete"
												>
													<Trash2 class="w-4 h-4" />
												</button>
											</div>
										{/if}
									</div>

									<!-- Prompt text -->
									<div class="generation-info">
										<p class="generation-prompt">
											{truncatePrompt(generation.prompt, 60)}
										</p>
										{#if generation.model}
											<p class="generation-model">{generation.model}</p>
										{/if}
									</div>
								</div>
							{/each}
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</div>
</div>

<style>
	/* Base cosmic history container */
	.cosmic-history {
		background: var(--card, oklch(0.18 0.008 260));
		position: relative;
	}

	/* Search container */
	.search-container {
		position: relative;
		display: flex;
		align-items: center;
	}

	.search-icon {
		position: absolute;
		left: 0.875rem;
		width: 1.125rem;
		height: 1.125rem;
		color: var(--muted-foreground);
		pointer-events: none;
		z-index: 1;
	}

	.search-input {
		width: 100%;
		height: 2.75rem;
		padding: 0 2.75rem 0 2.75rem;
		background: oklch(0.14 0.008 260 / 0.5);
		border: 1px solid oklch(0.72 0.14 180 / 0.15);
		border-radius: 0.75rem;
		color: var(--foreground);
		font-size: 0.875rem;
		transition: all 0.2s;
		backdrop-filter: blur(var(--glass-blur));
		-webkit-backdrop-filter: blur(var(--glass-blur));
	}

	.search-input::placeholder {
		color: var(--muted-foreground);
	}

	.search-input:focus {
		outline: none;
		border-color: oklch(0.72 0.14 180 / 0.4);
		background: oklch(0.16 0.008 260 / 0.7);
		box-shadow: 0 0 0 3px oklch(0.72 0.14 180 / 0.1);
	}

	.search-clear {
		position: absolute;
		right: 0.75rem;
		color: var(--muted-foreground);
		transition: color 0.2s;
	}

	.search-clear:hover {
		color: var(--foreground);
	}

	/* Filter chips */
	.filter-chip {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.5rem 0.875rem;
		background: oklch(0.14 0.008 260 / 0.5);
		border: 1px solid oklch(0.72 0.14 180 / 0.1);
		border-radius: 0.625rem;
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--muted-foreground);
		transition: all 0.2s;
		backdrop-filter: blur(var(--glass-blur));
		-webkit-backdrop-filter: blur(var(--glass-blur));
	}

	.filter-chip:hover {
		background: oklch(0.16 0.008 260 / 0.7);
		border-color: oklch(0.72 0.14 180 / 0.2);
		color: var(--foreground);
	}

	.filter-chip-active {
		background: oklch(0.72 0.14 180 / 0.15);
		border-color: oklch(0.72 0.14 180 / 0.4);
		color: var(--primary);
		box-shadow: 0 0 12px oklch(0.72 0.14 180 / 0.2);
	}

	.filter-count {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 1.25rem;
		height: 1.25rem;
		padding: 0 0.375rem;
		background: oklch(0.72 0.14 180 / 0.15);
		border-radius: 0.375rem;
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--primary);
	}

	/* Generation groups */
	.generation-group {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.group-header {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.group-title {
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.group-count {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 1.5rem;
		height: 1.5rem;
		padding: 0 0.375rem;
		background: oklch(0.72 0.14 180 / 0.1);
		border-radius: 0.375rem;
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--primary);
	}

	/* Generation grid */
	.generation-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
		gap: 0.875rem;
	}

	/* Generation card */
	.generation-card {
		display: flex;
		flex-direction: column;
		background: oklch(0.14 0.008 260 / 0.5);
		border: 1px solid oklch(0.72 0.14 180 / 0.1);
		border-radius: 0.875rem;
		overflow: hidden;
		transition: all 0.2s;
		cursor: pointer;
		backdrop-filter: blur(var(--glass-blur));
		-webkit-backdrop-filter: blur(var(--glass-blur));
	}

	.generation-card:hover:not(.generation-card-disabled) {
		border-color: oklch(0.72 0.14 180 / 0.3);
		box-shadow: 0 4px 16px oklch(0.72 0.14 180 / 0.15);
		transform: translateY(-2px);
	}

	.generation-card-hover {
		border-color: oklch(0.72 0.14 180 / 0.4);
	}

	.generation-card-disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	/* Thumbnail */
	.generation-thumb {
		position: relative;
		aspect-ratio: 16 / 9;
		background: oklch(0.12 0.008 260);
		overflow: hidden;
	}

	.generation-image {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}

	.generation-placeholder {
		width: 100%;
		height: 100%;
		display: flex;
		align-items: center;
		justify-content: center;
		background: oklch(0.12 0.008 260);
	}

	/* Type badge */
	.type-badge {
		position: absolute;
		top: 0.5rem;
		left: 0.5rem;
		display: flex;
		align-items: center;
		padding: 0.25rem 0.5rem;
		background: oklch(0.18 0.01 260 / 0.9);
		backdrop-filter: blur(var(--glass-blur));
		-webkit-backdrop-filter: blur(var(--glass-blur));
		border: 1px solid oklch(0.72 0.14 180 / 0.2);
		border-radius: 0.375rem;
		color: var(--primary);
	}

	/* Status indicator */
	.status-indicator {
		position: absolute;
		top: 0.5rem;
		right: 0.5rem;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 1.75rem;
		height: 1.75rem;
		background: oklch(0.18 0.01 260 / 0.9);
		backdrop-filter: blur(var(--glass-blur));
		-webkit-backdrop-filter: blur(var(--glass-blur));
		border: 1px solid oklch(0.72 0.14 180 / 0.2);
		border-radius: 0.375rem;
	}

	/* Progress bar */
	.progress-bar-container {
		position: absolute;
		bottom: 0;
		left: 0;
		right: 0;
		height: 0.25rem;
		background: oklch(0.18 0.01 260 / 0.5);
	}

	.progress-bar {
		height: 100%;
		background: var(--primary);
		transition: width 0.3s ease;
		box-shadow: 0 0 8px var(--primary);
	}

	/* Hover overlay */
	.hover-overlay {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 0.5rem;
		background: oklch(0.1 0.008 260 / 0.9);
		backdrop-filter: blur(4px);
		-webkit-backdrop-filter: blur(4px);
		animation: fade-in 0.15s ease;
	}

	@keyframes fade-in {
		from {
			opacity: 0;
		}
		to {
			opacity: 1;
		}
	}

	/* Small action buttons */
	.action-btn-small {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 2rem;
		height: 2rem;
		background: oklch(0.72 0.14 180 / 0.15);
		border: 1px solid oklch(0.72 0.14 180 / 0.3);
		border-radius: 0.5rem;
		color: var(--primary);
		transition: all 0.2s;
	}

	.action-btn-small:hover {
		background: oklch(0.72 0.14 180 / 0.25);
		border-color: oklch(0.72 0.14 180 / 0.5);
		transform: scale(1.1);
	}

	.action-btn-danger:hover {
		background: var(--destructive);
		border-color: var(--destructive);
		color: white;
	}

	/* Generation info */
	.generation-info {
		padding: 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.generation-prompt {
		font-size: 0.75rem;
		line-height: 1.3;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
	}

	.generation-model {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* Empty state */
	.empty-state-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 4rem;
		height: 4rem;
		background: oklch(0.72 0.14 180 / 0.1);
		border: 1px solid oklch(0.72 0.14 180 / 0.2);
		border-radius: 1rem;
	}

	/* Custom scrollbar */
	.custom-scrollbar {
		scrollbar-width: thin;
		scrollbar-color: oklch(0.72 0.14 180 / 0.3) transparent;
	}

	.custom-scrollbar::-webkit-scrollbar {
		width: 8px;
	}

	.custom-scrollbar::-webkit-scrollbar-track {
		background: transparent;
	}

	.custom-scrollbar::-webkit-scrollbar-thumb {
		background: oklch(0.72 0.14 180 / 0.3);
		border-radius: 4px;
	}

	.custom-scrollbar::-webkit-scrollbar-thumb:hover {
		background: oklch(0.72 0.14 180 / 0.5);
	}
</style>
