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

	// Swipe state - swipe to delete directly
	let touchStartX = 0;
	let touchStartY = 0;
	let currentSwipeX = 0;
	let directionLocked: 'horizontal' | 'vertical' | null = null;

	const DELETE_THRESHOLD = 80; // Swipe distance to trigger delete
	const MAX_SWIPE = 100;
	const DIRECTION_THRESHOLD = 10; // Pixels before locking direction

	function handleTouchStart(e: TouchEvent) {
		if (selectionMode) return;
		touchStartX = e.touches[0].clientX;
		touchStartY = e.touches[0].clientY;
		directionLocked = null;
		currentSwipeX = 0;
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

		// Only handle horizontal left swipes
		if (directionLocked === 'horizontal' && diffX > 0) {
			currentSwipeX = Math.min(diffX, MAX_SWIPE);
		}
	}

	function handleTouchEnd() {
		// Close or delete if swiped past threshold
		if (directionLocked === 'horizontal' && currentSwipeX >= DELETE_THRESHOLD) {
			// Open tabs get closed (unless streaming), history items get deleted
			if (isOpen && isStreaming) {
				// Don't allow closing streaming tabs
			} else {
				dispatch(isOpen ? 'close' : 'delete');
			}
		}

		// Reset
		currentSwipeX = 0;
		directionLocked = null;
	}

	function handleCardClick() {
		if (selectionMode) {
			dispatch('select');
		} else {
			dispatch('click');
		}
	}

	// Show delete indicator when past threshold
	$: isPastThreshold = currentSwipeX >= DELETE_THRESHOLD;

	function truncateTitle(title: string | null, maxLength: number = 40): string {
		if (!title) return 'New Chat';
		return title.length > maxLength ? title.substring(0, maxLength) + '...' : title;
	}

	// Determine status color - only show green when actively streaming
	$: statusColor = isStreaming
		? 'bg-primary animate-pulse'
		: session.status === 'error'
			? 'bg-destructive'
			: 'bg-muted-foreground/30';

	// Check if high cost
	$: isHighCost = (session.total_cost_usd ?? 0) > 10;
</script>

<div class="relative rounded-xl">
	<!-- Swipe indicator - sits behind the card -->
	{#if currentSwipeX > 0}
		{#if isOpen}
			<!-- Close indicator for open tabs (neutral color) -->
			<div class="absolute inset-0 flex justify-end items-center rounded-xl overflow-hidden {isPastThreshold ? 'bg-muted-foreground' : 'bg-muted-foreground/50'}">
				<div class="w-[100px] flex items-center justify-center">
					<svg class="w-5 h-5 text-background" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</div>
			</div>
		{:else}
			<!-- Delete indicator for history items (red) -->
			<div class="absolute inset-0 flex justify-end items-center rounded-xl overflow-hidden {isPastThreshold ? 'bg-destructive' : 'bg-destructive/50'}">
				<div class="w-[100px] flex items-center justify-center">
					<svg class="w-5 h-5 text-destructive-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
					</svg>
				</div>
			</div>
		{/if}
	{/if}

	<!-- Main card content - slides to reveal delete indicator -->
	<div
		class="group relative flex items-center gap-3 px-3 py-2.5 rounded-xl cursor-pointer select-none {isActive ? 'bg-white/10 border border-white/20' : 'hover:bg-white/5'} {selectionMode && isSelected ? 'bg-white/10' : ''}"
		class:transition-transform={!directionLocked}
		style="transform: translateX(-{currentSwipeX}px)"
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
				class="w-4 h-4 rounded border-border text-primary focus:ring-primary cursor-pointer flex-shrink-0"
			/>
		{/if}

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

		<!-- Active/Status indicator on right side (like reference image) -->
		<div class="flex items-center gap-2 flex-shrink-0">
			<!-- Activity indicator (green dot when active/streaming) -->
			{#if isActive || isStreaming}
				<span class="w-2.5 h-2.5 rounded-full bg-primary {isStreaming ? 'animate-pulse' : ''}"></span>
			{/if}

			<!-- Close/Delete button (desktop hover) -->
			{#if !selectionMode}
				{#if showCloseButton}
					<button
						on:click|stopPropagation={() => !isStreaming && dispatch('close')}
						class="opacity-0 group-hover:opacity-100 p-1 text-muted-foreground transition-opacity hidden sm:block {isStreaming ? 'cursor-not-allowed opacity-30' : 'hover:text-foreground'}"
						title={isStreaming ? "Can't close while streaming" : "Close tab"}
						disabled={isStreaming}
					>
						<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				{:else}
					<button
						on:click|stopPropagation={() => dispatch('delete')}
						class="opacity-0 group-hover:opacity-100 p-1 text-muted-foreground hover:text-foreground transition-opacity hidden sm:block"
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
</div>
