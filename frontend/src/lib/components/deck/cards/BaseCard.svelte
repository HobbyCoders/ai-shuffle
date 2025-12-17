<script lang="ts">
	/**
	 * BaseCard - Base card component for The Deck
	 *
	 * All card types extend this component which provides:
	 * - Header with title, pin, minimize, close buttons
	 * - Content slot
	 * - Footer slot (optional)
	 * - Drag handle for reordering
	 * - Visual states: active, inactive, minimized
	 * - Glassmorphism background with subtle shadow
	 */
	import type { Snippet } from 'svelte';
	import type { CardType } from './types';

	interface Props {
		id: string;
		title: string;
		type: CardType;
		pinned?: boolean;
		minimized?: boolean;
		active?: boolean;
		draggable?: boolean;
		children: Snippet;
		footer?: Snippet;
		headerActions?: Snippet;
		onpin?: () => void;
		onminimize?: () => void;
		onclose?: () => void;
		onactivate?: () => void;
		ondragstart?: (e: DragEvent) => void;
		ondragend?: (e: DragEvent) => void;
	}

	let {
		id,
		title,
		type,
		pinned = false,
		minimized = false,
		active = false,
		draggable = true,
		children,
		footer,
		headerActions,
		onpin,
		onminimize,
		onclose,
		onactivate,
		ondragstart,
		ondragend
	}: Props = $props();

	// Drag state
	let isDragging = $state(false);

	function handleDragStart(e: DragEvent) {
		isDragging = true;
		if (e.dataTransfer) {
			e.dataTransfer.effectAllowed = 'move';
			e.dataTransfer.setData('text/plain', id);
		}
		ondragstart?.(e);
	}

	function handleDragEnd(e: DragEvent) {
		isDragging = false;
		ondragend?.(e);
	}

	function handleCardClick() {
		if (!active) {
			onactivate?.();
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			handleCardClick();
		}
	}

	// Icon for card type
	function getTypeIcon(cardType: CardType): string {
		switch (cardType) {
			case 'chat':
				return 'M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z';
			case 'agent':
				return 'M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z';
			case 'canvas':
				return 'M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z';
			case 'terminal':
				return 'M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z';
			default:
				return 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z';
		}
	}
</script>

<div
	class="deck-card group relative flex flex-col rounded-2xl overflow-hidden transition-all duration-300
		{active ? 'deck-card--active ring-2 ring-primary/50 shadow-glow' : 'opacity-90 hover:opacity-100'}
		{minimized ? 'deck-card--minimized h-12' : 'h-full'}
		{isDragging ? 'deck-card--dragging opacity-50 scale-95' : ''}"
	role="button"
	tabindex="0"
	aria-label="{type} card: {title}"
	onclick={handleCardClick}
	onkeydown={handleKeydown}
	data-card-id={id}
	data-card-type={type}
>
	<!-- Glassmorphism background -->
	<div class="absolute inset-0 bg-card/80 backdrop-blur-xl border border-border/50 rounded-2xl pointer-events-none"></div>

	<!-- Active glow effect -->
	{#if active}
		<div class="absolute inset-0 bg-gradient-to-b from-primary/5 to-transparent rounded-2xl pointer-events-none"></div>
	{/if}

	<!-- Card Header -->
	<!-- svelte-ignore a11y_interactive_supports_focus -->
	<header
		class="relative flex items-center gap-2 px-3 py-2.5 border-b border-border/30 select-none shrink-0"
		draggable={draggable}
		ondragstart={handleDragStart}
		ondragend={handleDragEnd}
		role="toolbar"
		aria-label="Card controls"
	>
		<!-- Drag handle -->
		{#if draggable}
			<div class="flex-shrink-0 cursor-grab active:cursor-grabbing opacity-40 hover:opacity-70 transition-opacity" aria-hidden="true">
				<svg class="w-4 h-4 text-muted-foreground" fill="currentColor" viewBox="0 0 24 24">
					<circle cx="9" cy="6" r="1.5" />
					<circle cx="15" cy="6" r="1.5" />
					<circle cx="9" cy="12" r="1.5" />
					<circle cx="15" cy="12" r="1.5" />
					<circle cx="9" cy="18" r="1.5" />
					<circle cx="15" cy="18" r="1.5" />
				</svg>
			</div>
		{/if}

		<!-- Card type icon -->
		<div class="flex-shrink-0 w-6 h-6 rounded-md bg-primary/10 flex items-center justify-center">
			<svg class="w-3.5 h-3.5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={getTypeIcon(type)} />
			</svg>
		</div>

		<!-- Title -->
		<h3 class="flex-1 text-sm font-medium text-foreground truncate">{title}</h3>

		<!-- Custom header actions slot -->
		{#if headerActions}
			<div class="flex items-center gap-1">
				{@render headerActions()}
			</div>
		{/if}

		<!-- Pin button -->
		<button
			type="button"
			class="p-1.5 rounded-lg transition-colors {pinned ? 'text-primary bg-primary/10' : 'text-muted-foreground hover:text-foreground hover:bg-muted opacity-0 group-hover:opacity-100'}"
			onclick={(e) => { e.stopPropagation(); onpin?.(); }}
			aria-label={pinned ? 'Unpin card' : 'Pin card'}
			aria-pressed={pinned}
			title={pinned ? 'Unpin' : 'Pin'}
		>
			<svg class="w-4 h-4" fill={pinned ? 'currentColor' : 'none'} stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 5a2 2 0 012-2h10a2 2 0 012 2v16l-7-3.5L5 21V5z" />
			</svg>
		</button>

		<!-- Minimize button -->
		<button
			type="button"
			class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors opacity-0 group-hover:opacity-100"
			onclick={(e) => { e.stopPropagation(); onminimize?.(); }}
			aria-label={minimized ? 'Expand card' : 'Minimize card'}
			aria-expanded={!minimized}
			title={minimized ? 'Expand' : 'Minimize'}
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				{#if minimized}
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
				{:else}
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 12H4" />
				{/if}
			</svg>
		</button>

		<!-- Close button -->
		<button
			type="button"
			class="p-1.5 rounded-lg text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors opacity-0 group-hover:opacity-100"
			onclick={(e) => { e.stopPropagation(); onclose?.(); }}
			aria-label="Close card"
			title="Close"
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
			</svg>
		</button>
	</header>

	<!-- Card Content -->
	{#if !minimized}
		<main class="relative flex-1 overflow-hidden">
			{@render children()}
		</main>

		<!-- Card Footer (optional) -->
		{#if footer}
			<footer class="relative border-t border-border/30 shrink-0">
				{@render footer()}
			</footer>
		{/if}
	{/if}
</div>

<style>
	.deck-card {
		/* Base card styling */
		box-shadow:
			0 4px 6px -1px rgba(0, 0, 0, 0.1),
			0 2px 4px -1px rgba(0, 0, 0, 0.06),
			0 0 0 1px var(--border);
	}

	.deck-card--active {
		box-shadow:
			0 10px 15px -3px rgba(0, 0, 0, 0.1),
			0 4px 6px -2px rgba(0, 0, 0, 0.05),
			0 0 20px var(--glow-color-soft),
			0 0 0 1px var(--primary);
	}

	.deck-card--minimized {
		box-shadow:
			0 1px 3px 0 rgba(0, 0, 0, 0.1),
			0 1px 2px 0 rgba(0, 0, 0, 0.06);
	}

	.deck-card--dragging {
		box-shadow:
			0 25px 50px -12px rgba(0, 0, 0, 0.25);
	}
</style>
