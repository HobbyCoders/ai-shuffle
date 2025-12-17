<script lang="ts">
	/**
	 * GenerationHistory - Recent generations carousel component
	 *
	 * Provides:
	 * - Horizontal scroll of recent items
	 * - Click to load into preview
	 * - Quick re-generate button
	 * - Delete button on hover
	 * - Shows prompt preview on hover
	 */
	import { createEventDispatcher } from 'svelte';
	import { studio } from '$lib/stores/studio';
	import type { Generation } from '$lib/stores/studio';

	interface Props {
		generations: Generation[];
		maxItems?: number;
	}

	let {
		generations,
		maxItems = 20
	}: Props = $props();

	const dispatch = createEventDispatcher<{
		click: { generation: Generation };
		regenerate: { generation: Generation };
		delete: { generation: Generation };
	}>();

	// Truncate generations
	const displayedGenerations = $derived(generations.slice(0, maxItems));

	// Scroll container ref
	let scrollContainer: HTMLDivElement | null = $state(null);
	let showLeftArrow = $state(false);
	let showRightArrow = $state(true);

	function handleScroll() {
		if (!scrollContainer) return;

		const { scrollLeft, scrollWidth, clientWidth } = scrollContainer;
		showLeftArrow = scrollLeft > 20;
		showRightArrow = scrollLeft < scrollWidth - clientWidth - 20;
	}

	function scrollLeft() {
		if (!scrollContainer) return;
		scrollContainer.scrollBy({ left: -200, behavior: 'smooth' });
	}

	function scrollRight() {
		if (!scrollContainer) return;
		scrollContainer.scrollBy({ left: 200, behavior: 'smooth' });
	}

	function handleClick(gen: Generation) {
		dispatch('click', { generation: gen });
	}

	function handleRegenerate(e: Event, gen: Generation) {
		e.stopPropagation();
		dispatch('regenerate', { generation: gen });
	}

	function handleDelete(e: Event, gen: Generation) {
		e.stopPropagation();
		// Remove from store - the store will update the list
		studio.setActiveGeneration(null);
		dispatch('delete', { generation: gen });
	}

	// Status styles
	function getStatusStyles(status: string): { bg: string; ring: string; icon: string } {
		switch (status) {
			case 'generating':
			case 'pending':
				return {
					bg: 'bg-primary/20',
					ring: 'ring-primary/30',
					icon: 'animate-spin'
				};
			case 'completed':
				return {
					bg: 'bg-green-500/20',
					ring: 'ring-green-500/30',
					icon: ''
				};
			case 'failed':
				return {
					bg: 'bg-destructive/20',
					ring: 'ring-destructive/30',
					icon: ''
				};
			default:
				return {
					bg: 'bg-muted',
					ring: 'ring-border',
					icon: ''
				};
		}
	}

	// Format time ago
	function timeAgo(date: Date): string {
		const seconds = Math.floor((Date.now() - date.getTime()) / 1000);

		if (seconds < 60) return 'Just now';
		if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
		if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
		return `${Math.floor(seconds / 86400)}d ago`;
	}

	// Truncate prompt
	function truncatePrompt(prompt: string, maxLength = 50): string {
		if (prompt.length <= maxLength) return prompt;
		return prompt.slice(0, maxLength).trim() + '...';
	}
</script>

<div class="generation-history relative">
	<!-- Header -->
	<div class="flex items-center justify-between mb-3">
		<h4 class="text-sm font-medium text-foreground">Recent Generations</h4>
		<span class="text-xs text-muted-foreground">
			{generations.length} total
		</span>
	</div>

	<!-- Carousel container -->
	<div class="relative">
		<!-- Left scroll button -->
		{#if showLeftArrow}
			<button
				type="button"
				class="absolute left-0 top-1/2 -translate-y-1/2 z-10 w-8 h-8 rounded-full bg-background/90 border border-border/50 shadow-lg flex items-center justify-center text-muted-foreground hover:text-foreground transition-colors"
				onclick={scrollLeft}
				aria-label="Scroll left"
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
				</svg>
			</button>
		{/if}

		<!-- Right scroll button -->
		{#if showRightArrow && displayedGenerations.length > 3}
			<button
				type="button"
				class="absolute right-0 top-1/2 -translate-y-1/2 z-10 w-8 h-8 rounded-full bg-background/90 border border-border/50 shadow-lg flex items-center justify-center text-muted-foreground hover:text-foreground transition-colors"
				onclick={scrollRight}
				aria-label="Scroll right"
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
				</svg>
			</button>
		{/if}

		<!-- Scrollable container -->
		<div
			bind:this={scrollContainer}
			onscroll={handleScroll}
			class="flex gap-3 overflow-x-auto pb-2 scrollbar-thin scrollbar-thumb-muted scrollbar-track-transparent"
		>
			{#if displayedGenerations.length === 0}
				<div class="flex-1 py-8 text-center">
					<p class="text-sm text-muted-foreground">No generations yet</p>
				</div>
			{:else}
				{#each displayedGenerations as gen (gen.id)}
					{@const statusStyles = getStatusStyles(gen.status)}
					<!-- svelte-ignore a11y_click_events_have_key_events -->
					<div
						class="group relative flex-shrink-0 w-36 cursor-pointer"
						onclick={() => handleClick(gen)}
						role="button"
						tabindex="0"
						onkeydown={(e) => e.key === 'Enter' && handleClick(gen)}
					>
						<!-- Thumbnail -->
						<div class="relative aspect-square rounded-xl overflow-hidden border border-border/50 ring-2 {statusStyles.ring} transition-all group-hover:ring-primary/50 group-hover:border-primary/30">
							{#if gen.status === 'completed' && gen.resultUrl}
								{#if gen.type === 'video'}
									<video
										src={gen.resultUrl}
										class="w-full h-full object-cover"
										muted
										loop
										playsinline
									>
										<track kind="captions" />
									</video>
									<div class="absolute inset-0 flex items-center justify-center bg-black/20">
										<div class="w-8 h-8 rounded-full bg-white/20 backdrop-blur-sm flex items-center justify-center">
											<svg class="w-4 h-4 text-white" fill="currentColor" viewBox="0 0 24 24">
												<path d="M5 3l14 9-14 9V3z" />
											</svg>
										</div>
									</div>
								{:else}
									<img
										src={gen.resultUrl}
										alt={gen.prompt}
										class="w-full h-full object-cover"
									/>
								{/if}
							{:else if gen.status === 'generating' || gen.status === 'pending'}
								<!-- Loading state -->
								<div class="absolute inset-0 flex items-center justify-center {statusStyles.bg}">
									<svg class="w-8 h-8 text-primary {statusStyles.icon}" fill="none" viewBox="0 0 24 24">
										<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
										<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
									</svg>
								</div>
								<!-- Progress indicator -->
								{#if gen.progress !== undefined && gen.progress > 0}
									<div class="absolute bottom-0 left-0 right-0 h-1 bg-muted">
										<div
											class="h-full bg-primary transition-all duration-300"
											style="width: {Math.round(gen.progress * 100)}%"
										></div>
									</div>
								{/if}
							{:else if gen.status === 'failed'}
								<!-- Failed state -->
								<div class="absolute inset-0 flex items-center justify-center {statusStyles.bg}">
									<svg class="w-8 h-8 text-destructive" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
									</svg>
								</div>
							{:else}
								<!-- Placeholder -->
								<div class="absolute inset-0 flex items-center justify-center bg-muted/30">
									<svg class="w-8 h-8 text-muted-foreground/50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
									</svg>
								</div>
							{/if}

							<!-- Type badge -->
							<div class="absolute top-1.5 left-1.5 px-1.5 py-0.5 rounded text-[10px] font-medium bg-black/50 text-white">
								{gen.type === 'video' ? 'VID' : 'IMG'}
							</div>

							<!-- Hover actions -->
							<div class="absolute inset-0 bg-black/50 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center gap-2">
								{#if gen.status === 'completed'}
									<button
										type="button"
										class="p-1.5 rounded-lg bg-white/20 text-white hover:bg-white/30 transition-colors"
										onclick={(e) => handleRegenerate(e, gen)}
										title="Regenerate"
									>
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
										</svg>
									</button>
								{/if}
								<button
									type="button"
									class="p-1.5 rounded-lg bg-white/20 text-white hover:bg-destructive/80 transition-colors"
									onclick={(e) => handleDelete(e, gen)}
									title="Delete"
								>
									<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
									</svg>
								</button>
							</div>
						</div>

						<!-- Info -->
						<div class="mt-2">
							<p class="text-xs text-foreground truncate" title={gen.prompt}>
								{truncatePrompt(gen.prompt, 30)}
							</p>
							<p class="text-[10px] text-muted-foreground">
								{timeAgo(gen.createdAt)}
							</p>
						</div>
					</div>
				{/each}
			{/if}
		</div>
	</div>
</div>

<style>
	.generation-history {
		/* Container styles */
	}

	/* Custom scrollbar */
	.scrollbar-thin {
		scrollbar-width: thin;
	}

	.scrollbar-thin::-webkit-scrollbar {
		height: 6px;
	}

	.scrollbar-thin::-webkit-scrollbar-track {
		background: transparent;
	}

	.scrollbar-thin::-webkit-scrollbar-thumb {
		background-color: var(--muted);
		border-radius: 3px;
	}

	.scrollbar-thin::-webkit-scrollbar-thumb:hover {
		background-color: var(--muted-foreground);
	}
</style>
