<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { Session } from '$lib/api/client';
	import { formatRelativeTime, formatCost, formatTimeOfDay, getStatusDisplay } from '$lib/utils/dateGroups';

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

	// Get status display info
	$: statusDisplay = getStatusDisplay(session.status, isStreaming);

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
		class="group relative flex flex-col gap-1.5 px-3 py-2.5 rounded-xl cursor-pointer select-none {isActive ? 'bg-white/10 border border-white/20' : 'hover:bg-white/5'} {selectionMode && isSelected ? 'bg-white/10' : ''}"
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
		<!-- Top row: Status + Title + Time -->
		<div class="flex items-center justify-between gap-2">
			<div class="flex items-center gap-2 min-w-0 flex-1">
				<!-- Checkbox for selection mode -->
				{#if selectionMode}
					<input
						type="checkbox"
						checked={isSelected}
						on:click|stopPropagation={() => dispatch('select')}
						class="w-4 h-4 rounded border-border text-primary focus:ring-primary cursor-pointer flex-shrink-0"
					/>
				{/if}

				<!-- Status indicator dot + label -->
				{#if statusDisplay}
					<span class="w-2 h-2 rounded-full flex-shrink-0 {statusDisplay.color} {isStreaming ? 'animate-pulse' : ''}"></span>
					<span class="text-xs text-muted-foreground flex-shrink-0">{statusDisplay.label}</span>
				{/if}

				<!-- Title -->
				<p class="text-sm font-medium text-foreground truncate leading-snug">
					{truncateTitle(session.title)}
				</p>
			</div>

			<!-- Time of day -->
			<span class="text-xs text-muted-foreground flex-shrink-0">
				{formatTimeOfDay(session.updated_at)}
			</span>
		</div>

		<!-- Middle row: Description/subtitle (using title if long, or placeholder) -->
		<p class="text-xs text-muted-foreground truncate leading-relaxed">
			{#if session.title && session.title.length > 30}
				{session.title}
			{:else}
				<!-- Placeholder - could be enhanced to show first message preview -->
				Session with {session.turn_count} {session.turn_count === 1 ? 'turn' : 'turns'}
			{/if}
		</p>

		<!-- Bottom row: Message count badge + Time ago + Cost -->
		<div class="flex items-center gap-2 mt-0.5">
			<!-- Message count badge -->
			<div class="flex items-center gap-1 bg-primary/20 text-primary px-1.5 py-0.5 rounded-md">
				<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
				</svg>
				<span class="text-xs font-medium">{session.turn_count}</span>
			</div>

			<!-- Time ago -->
			<span class="text-xs text-muted-foreground">
				{formatRelativeTime(session.updated_at, abbreviated)}
			</span>

			<!-- Cost -->
			{#if session.total_cost_usd}
				<span class="text-xs {isHighCost ? 'text-warning' : 'text-success'}">
					{formatCost(session.total_cost_usd, abbreviated)}
				</span>
			{/if}

			<!-- Spacer -->
			<div class="flex-1"></div>

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
