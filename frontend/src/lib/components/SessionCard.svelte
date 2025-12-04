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

	// Swipe state - "snap open" approach
	let isSwipedOpen = false;
	let touchStartX = 0;
	let touchStartY = 0;
	let currentSwipeX = 0;
	let directionLocked: 'horizontal' | 'vertical' | null = null;

	const DELETE_BUTTON_WIDTH = 72; // Width of delete button area
	const DIRECTION_THRESHOLD = 8; // Pixels before locking direction

	function handleTouchStart(e: TouchEvent) {
		if (selectionMode) return;
		touchStartX = e.touches[0].clientX;
		touchStartY = e.touches[0].clientY;
		directionLocked = null;

		// If swiped open, adjust start position
		if (isSwipedOpen) {
			touchStartX += DELETE_BUTTON_WIDTH;
		}
	}

	function handleTouchMove(e: TouchEvent) {
		if (selectionMode) return;

		const currentX = e.touches[0].clientX;
		const currentY = e.touches[0].clientY;
		const diffX = touchStartX - currentX;
		const diffY = currentY - touchStartY;

		// Lock direction on first significant movement
		if (directionLocked === null) {
			if (Math.abs(diffX) > DIRECTION_THRESHOLD || Math.abs(diffY) > DIRECTION_THRESHOLD) {
				directionLocked = Math.abs(diffX) > Math.abs(diffY) ? 'horizontal' : 'vertical';
			}
		}

		// Only handle horizontal swipes
		if (directionLocked === 'horizontal') {
			// Clamp between 0 and DELETE_BUTTON_WIDTH
			currentSwipeX = Math.max(0, Math.min(diffX, DELETE_BUTTON_WIDTH));
		}
	}

	function handleTouchEnd() {
		if (directionLocked === 'horizontal') {
			// Snap open if swiped more than halfway, otherwise snap closed
			if (currentSwipeX > DELETE_BUTTON_WIDTH / 2) {
				isSwipedOpen = true;
			} else {
				isSwipedOpen = false;
			}
		}

		currentSwipeX = 0;
		directionLocked = null;
	}

	function handleDelete() {
		isSwipedOpen = false;
		dispatch('delete');
	}

	function handleCardClick() {
		if (isSwipedOpen) {
			// Close swipe if open
			isSwipedOpen = false;
		} else if (selectionMode) {
			dispatch('select');
		} else {
			dispatch('click');
		}
	}

	// Calculate transform based on state
	$: swipeTransform = isSwipedOpen ? DELETE_BUTTON_WIDTH : currentSwipeX;

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
	<!-- Delete button (revealed on swipe) -->
	<button
		class="absolute inset-y-0 right-0 w-[72px] bg-destructive flex items-center justify-center transition-opacity active:bg-destructive/80"
		class:opacity-100={swipeTransform > 0}
		class:opacity-0={swipeTransform === 0}
		on:click|stopPropagation={handleDelete}
		aria-label="Delete session"
	>
		<svg class="w-5 h-5 text-destructive-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
		</svg>
	</button>

	<!-- Main card content -->
	<div
		class="group relative flex items-start gap-3 px-3 py-2.5 rounded-lg cursor-pointer select-none bg-card {isActive ? 'bg-primary/20 border border-primary/30' : 'hover:bg-accent'} {selectionMode && isSelected ? 'bg-accent/50' : ''}"
		class:transition-transform={!directionLocked}
		style="transform: translateX(-{swipeTransform}px)"
		on:click={handleCardClick}
		on:keypress={(e) => e.key === 'Enter' && handleCardClick()}
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
