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
		favorite: void;
		openTagPicker: { x: number; y: number };
	}>();

	// Check if session has tags (with fallback for older sessions)
	// Filter out any malformed tags that might be missing required fields
	$: validTags = (session.tags || []).filter(tag => tag && tag.id && tag.name);
	$: hasTags = validTags.length > 0;
	$: displayTags = validTags.slice(0, 3); // Show max 3 tags
	$: moreTags = validTags.length - 3;

	function handleTagClick(e: MouseEvent) {
		e.stopPropagation();
		const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
		dispatch('openTagPicker', { x: rect.left, y: rect.bottom + 4 });
	}

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
	$: statusDisplay = getStatusDisplay(session.status, isStreaming, isOpen);

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
		class="group relative flex flex-col gap-1.5 px-3 py-2.5 rounded-xl cursor-pointer select-none {isActive ? 'bg-accent border border-border' : 'hover:bg-accent/50'} {selectionMode && isSelected ? 'bg-accent' : ''}"
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

				<!-- Favorite star button -->
				<button
					on:click|stopPropagation={() => dispatch('favorite')}
					class="flex-shrink-0 p-0.5 rounded transition-colors {session.is_favorite ? 'text-yellow-400 hover:text-yellow-500' : 'text-muted-foreground/40 hover:text-yellow-400 opacity-0 group-hover:opacity-100'}"
					title={session.is_favorite ? 'Remove from favorites' : 'Add to favorites'}
				>
					<svg class="w-4 h-4" viewBox="0 0 24 24" fill={session.is_favorite ? 'currentColor' : 'none'} stroke="currentColor" stroke-width="2">
						<path stroke-linecap="round" stroke-linejoin="round" d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.197-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.784-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z" />
					</svg>
				</button>

				<!-- Fork indicator -->
				{#if session.parent_session_id}
					<span
						class="flex-shrink-0 text-purple-400"
						title="Forked session"
					>
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7v8a2 2 0 002 2h6M8 7V5a2 2 0 012-2h4.586a1 1 0 01.707.293l4.414 4.414a1 1 0 01.293.707V15a2 2 0 01-2 2h-2M8 7H6a2 2 0 00-2 2v10a2 2 0 002 2h8a2 2 0 002-2v-2" />
						</svg>
					</span>
				{/if}

				<!-- Has forks indicator -->
				{#if session.has_forks}
					<span
						class="flex-shrink-0 text-blue-400"
						title="Has forked sessions"
					>
						<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6" />
						</svg>
					</span>
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

		<!-- Bottom row: Message count badge + Tags + Time ago + Cost -->
		<div class="flex items-center gap-2 mt-0.5 flex-wrap">
			<!-- Message count badge -->
			<div class="flex items-center gap-1 bg-primary/20 text-primary px-1.5 py-0.5 rounded-md">
				<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
				</svg>
				<span class="text-xs font-medium">{session.turn_count}</span>
			</div>

			<!-- Tags -->
			{#if hasTags}
				<div class="flex items-center gap-1">
					{#each displayTags as tag (tag.id)}
						<span
							class="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium text-white/90"
							style="background-color: {tag.color}"
							title={tag.name}
						>
							{tag.name.length > 8 ? tag.name.slice(0, 8) + '...' : tag.name}
						</span>
					{/each}
					{#if moreTags > 0}
						<span class="text-xs text-muted-foreground">+{moreTags}</span>
					{/if}
				</div>
			{/if}

			<!-- Tag button (shows on hover) -->
			<button
				on:click={handleTagClick}
				class="opacity-0 group-hover:opacity-100 p-0.5 rounded text-muted-foreground hover:text-foreground hover:bg-accent transition-all"
				title="Manage tags"
			>
				<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" />
				</svg>
			</button>

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
