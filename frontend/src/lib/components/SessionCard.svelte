<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { Session } from '$lib/api/client';
	import { formatRelativeTime, formatCost, formatTurns } from '$lib/utils/dateGroups';

	export let session: Session;
	export let isOpen = false;
	export let isActive = false;
	export let isStreaming = false;
	export let selectionMode = false;
	export let isSelected = false;
	export let showCloseButton = false;
	export let abbreviated = false; // For mobile view

	const dispatch = createEventDispatcher<{
		click: void;
		delete: void;
		close: void;
		select: void;
	}>();

	// Swipe gesture state
	let touchStartX = 0;
	let touchCurrentX = 0;
	let isSwiping = false;
	let swipeOffset = 0;
	let showDeleteAction = false;

	const SWIPE_THRESHOLD = 80;
	const MAX_SWIPE = 100;

	function handleTouchStart(e: TouchEvent) {
		if (selectionMode) return;
		touchStartX = e.touches[0].clientX;
		touchCurrentX = touchStartX;
		isSwiping = true;
	}

	function handleTouchMove(e: TouchEvent) {
		if (!isSwiping || selectionMode) return;
		touchCurrentX = e.touches[0].clientX;
		const diff = touchStartX - touchCurrentX;

		// Only allow left swipe (positive diff)
		if (diff > 0) {
			swipeOffset = Math.min(diff, MAX_SWIPE);
			showDeleteAction = diff > SWIPE_THRESHOLD;
		} else {
			swipeOffset = 0;
			showDeleteAction = false;
		}
	}

	function handleTouchEnd() {
		if (!isSwiping) return;
		isSwiping = false;

		if (showDeleteAction) {
			dispatch('delete');
		}

		// Reset swipe
		swipeOffset = 0;
		showDeleteAction = false;
	}

	function truncateTitle(title: string | null, maxLength: number = 40): string {
		if (!title) return 'New Chat';
		return title.length > maxLength ? title.substring(0, maxLength) + '...' : title;
	}

	// Determine status color
	$: statusColor = isStreaming
		? 'bg-primary animate-pulse'
		: session.status === 'error'
			? 'bg-destructive'
			: isOpen || isActive
				? 'bg-primary'
				: 'bg-muted-foreground/30';

	// Check if high cost
	$: isHighCost = (session.total_cost_usd ?? 0) > 10;
</script>

<div class="relative overflow-hidden rounded-lg">
	<!-- Delete action background (revealed on swipe) -->
	<div
		class="absolute inset-y-0 right-0 w-24 bg-destructive flex items-center justify-end pr-4 transition-opacity"
		class:opacity-100={swipeOffset > 0}
		class:opacity-0={swipeOffset === 0}
	>
		<svg class="w-5 h-5 text-destructive-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
		</svg>
	</div>

	<!-- Main card content -->
	<div
		class="group relative flex items-start gap-3 px-3 py-2.5 rounded-lg cursor-pointer transition-all touch-pan-y select-none {isActive ? 'bg-primary/20 border border-primary/30' : 'hover:bg-accent'} {selectionMode && isSelected ? 'bg-accent/50' : ''}"
		style="transform: translateX(-{swipeOffset}px)"
		on:click={() => selectionMode ? dispatch('select') : dispatch('click')}
		on:keypress={(e) => e.key === 'Enter' && (selectionMode ? dispatch('select') : dispatch('click'))}
		on:touchstart={handleTouchStart}
		on:touchmove={handleTouchMove}
		on:touchend={handleTouchEnd}
		role="button"
		tabindex="0"
	>
		<!-- Checkbox for selection mode -->
		{#if selectionMode}
			<input
				type="checkbox"
				checked={isSelected}
				on:click|stopPropagation={() => dispatch('select')}
				class="w-4 h-4 mt-0.5 rounded border-border text-primary focus:ring-primary cursor-pointer flex-shrink-0"
			/>
		{/if}

		<!-- Status indicator -->
		<span class="w-2 h-2 mt-1.5 rounded-full flex-shrink-0 {statusColor}"></span>

		<!-- Content -->
		<div class="flex-1 min-w-0">
			<!-- Title -->
			<p class="text-sm text-foreground truncate leading-snug">
				{truncateTitle(session.title)}
			</p>

			<!-- Meta row -->
			<div class="flex items-center gap-1.5 mt-0.5 text-xs text-muted-foreground">
				<!-- Time -->
				<span>{formatRelativeTime(session.updated_at, abbreviated)}</span>

				<span class="opacity-50">·</span>

				<!-- Turn count -->
				<span>{formatTurns(session.turn_count, abbreviated)}</span>

				<!-- Cost (if exists) -->
				{#if session.total_cost_usd}
					<span class="opacity-50">·</span>
					<span class:text-warning={isHighCost} class:text-success={!isHighCost}>
						{formatCost(session.total_cost_usd, abbreviated)}
					</span>
				{/if}
			</div>
		</div>

		<!-- Close/Delete button (desktop hover) -->
		{#if !selectionMode}
			{#if showCloseButton}
				<button
					on:click|stopPropagation={() => dispatch('close')}
					class="opacity-0 group-hover:opacity-100 p-1 text-muted-foreground hover:text-destructive transition-opacity flex-shrink-0 hidden sm:block"
					title="Close tab"
				>
					<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			{:else}
				<button
					on:click|stopPropagation={() => dispatch('delete')}
					class="opacity-0 group-hover:opacity-100 p-1 text-muted-foreground hover:text-destructive transition-opacity flex-shrink-0 hidden sm:block"
					title="Delete session"
				>
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			{/if}
		{/if}
	</div>
</div>
