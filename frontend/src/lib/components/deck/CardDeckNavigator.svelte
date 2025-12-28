<script lang="ts">
	/**
	 * CardDeckNavigator - AI Shuffle themed card navigation overlay
	 *
	 * Full-screen card carousel for browsing and managing:
	 * - Quick actions (new chat, new agent, terminal, etc.)
	 * - Recent threads/conversations
	 * - Projects and their threads
	 * - Studio tools
	 *
	 * Features:
	 * - Horizontal scroll with snap
	 * - Sub-deck drill-down with breadcrumb navigation
	 * - Mobile swipe gestures
	 * - AI streaming state indicators
	 */

	import { MessageSquare, Terminal, Bot, Clock, Image, Box, AudioLines, Settings, FolderOpen, X, Cpu, Files, User } from 'lucide-svelte';
	import { tabs, type Session } from '$lib/stores/tabs';
	import { deck, type DeckCard as DeckCardType } from '$lib/stores/deck';
	import { fly, fade } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';

	interface Props {
		open: boolean;
		onClose: () => void;
		onCreateChat: () => void;
		onCreateAgent: () => void;
		onCreateTerminal: () => void;
		onOpenThread: (sessionId: string) => void;
		onOpenImageStudio?: () => void;
		onOpenModelStudio?: () => void;
		onOpenAudioStudio?: () => void;
		onOpenFileBrowser?: () => void;
		onOpenProjects?: () => void;
		onOpenProfiles?: () => void;
		onOpenSettings?: () => void;
		isAdmin?: boolean;
		isMobile?: boolean;
	}

	let {
		open,
		onClose,
		onCreateChat,
		onCreateAgent,
		onCreateTerminal,
		onOpenThread,
		onOpenImageStudio,
		onOpenModelStudio,
		onOpenAudioStudio,
		onOpenFileBrowser,
		onOpenProjects,
		onOpenProfiles,
		onOpenSettings,
		isAdmin = false,
		isMobile = false
	}: Props = $props();

	// Navigation stack for breadcrumb
	interface NavItem {
		id: string;
		name: string;
		type: 'root' | 'category' | 'project';
	}

	let navStack = $state<NavItem[]>([{ id: 'root', name: 'AI Shuffle', type: 'root' }]);
	let currentView = $state<'main' | 'recent'>('main');

	// Carousel state
	let carouselRef = $state<HTMLDivElement | null>(null);
	let isDragging = $state(false);
	let hasDragged = $state(false); // Track if user actually dragged (moved > threshold)
	let startX = $state(0);
	let scrollLeft = $state(0);
	let scrollProgress = $state(0);
	const DRAG_THRESHOLD = 5; // pixels before considering it a drag

	// Card types for main deck
	type CardAction = 'new-chat' | 'new-agent' | 'terminal' | 'recent' | 'image-studio' | 'model-studio' | 'audio-studio' | 'file-browser' | 'projects' | 'profiles' | 'settings';

	interface NavigatorCard {
		id: string;
		type: 'action' | 'category' | 'thread';
		action?: CardAction;
		category?: string;
		title: string;
		subtitle: string;
		icon: typeof MessageSquare;
		hasChildren?: boolean;
		// Thread-specific
		sessionId?: string;
		isStreaming?: boolean;
		messageCount?: number;
		lastActive?: string;
	}

	// Get sessions from tabs store
	const sessions = $derived($tabs.sessions);

	// Get active streaming cards
	const activeCards = $derived($deck.cards.filter(c => c.type === 'chat'));

	// Check if a session is currently streaming
	function isSessionStreaming(sessionId: string): boolean {
		const tab = $tabs.tabs.find(t => t.sessionId === sessionId);
		return tab?.isStreaming ?? false;
	}

	// Format relative time
	function formatRelativeTime(dateStr: string): string {
		const date = new Date(dateStr);
		const now = new Date();
		const diffMs = now.getTime() - date.getTime();
		const diffMins = Math.floor(diffMs / 60000);
		const diffHours = Math.floor(diffMs / 3600000);
		const diffDays = Math.floor(diffMs / 86400000);

		if (diffMins < 1) return 'Just now';
		if (diffMins < 60) return `${diffMins}m ago`;
		if (diffHours < 24) return `${diffHours}h ago`;
		if (diffDays < 7) return `${diffDays}d ago`;
		return date.toLocaleDateString();
	}

	// Build main deck cards
	const mainDeckCards = $derived.by((): NavigatorCard[] => {
		const cards: NavigatorCard[] = [
			{
				id: 'new-chat',
				type: 'action',
				action: 'new-chat',
				title: 'New Chat',
				subtitle: 'Start a conversation with AI',
				icon: MessageSquare
			},
			{
				id: 'new-agent',
				type: 'action',
				action: 'new-agent',
				title: 'New Agent',
				subtitle: 'Autonomous background task',
				icon: Cpu
			},
			{
				id: 'terminal',
				type: 'action',
				action: 'terminal',
				title: 'Terminal',
				subtitle: 'Command line interface',
				icon: Terminal
			},
			{
				id: 'recent',
				type: 'category',
				category: 'recent',
				title: 'Recent',
				subtitle: `${sessions.length} conversations`,
				icon: Clock,
				hasChildren: true
			},
			{
				id: 'image-studio',
				type: 'action',
				action: 'image-studio',
				title: 'Image Studio',
				subtitle: 'Generate & edit images',
				icon: Image
			},
			{
				id: 'model-studio',
				type: 'action',
				action: 'model-studio',
				title: '3D Models',
				subtitle: 'Generate 3D assets',
				icon: Box
			},
			{
				id: 'audio-studio',
				type: 'action',
				action: 'audio-studio',
				title: 'Audio Studio',
				subtitle: 'Text to speech & more',
				icon: AudioLines
			},
			{
				id: 'file-browser',
				type: 'action',
				action: 'file-browser',
				title: 'Files',
				subtitle: 'Browse & manage files',
				icon: Files
			}
		];

		// Add projects card (opens project manager directly)
		cards.push({
			id: 'projects',
			type: 'action',
			action: 'projects',
			title: 'Projects',
			subtitle: 'Manage workspaces',
			icon: FolderOpen
		});

		// Add profiles card
		cards.push({
			id: 'profiles',
			type: 'action',
			action: 'profiles',
			title: 'Profiles',
			subtitle: 'Agent configurations',
			icon: User
		});

		// Add settings for admin
		if (isAdmin) {
			cards.push({
				id: 'settings',
				type: 'action',
				action: 'settings',
				title: 'Settings',
				subtitle: 'Preferences & config',
				icon: Settings
			});
		}

		return cards;
	});

	// Build recent threads cards
	const recentThreadCards = $derived.by((): NavigatorCard[] => {
		return sessions
			.slice(0, 20) // Limit to 20 recent
			.map((session: Session): NavigatorCard => ({
				id: `thread-${session.id}`,
				type: 'thread',
				title: session.title || 'Untitled',
				subtitle: '',
				icon: MessageSquare,
				sessionId: session.id,
				isStreaming: isSessionStreaming(session.id),
				messageCount: session.message_count,
				lastActive: formatRelativeTime(session.updated_at)
			}));
	});

	// Get current deck of cards based on view
	const currentCards = $derived.by((): NavigatorCard[] => {
		switch (currentView) {
			case 'recent':
				return recentThreadCards;
			default:
				return mainDeckCards;
		}
	});

	// Handle card click
	function handleCardClick(card: NavigatorCard) {
		// Only block click if user actually dragged
		if (hasDragged) return;

		if (card.type === 'action' && card.action) {
			handleAction(card.action);
		} else if (card.type === 'category' && card.category) {
			navigateToCategory(card);
		} else if (card.type === 'thread' && card.sessionId) {
			onOpenThread(card.sessionId);
			onClose();
		}
	}

	// Handle action cards
	function handleAction(action: CardAction) {
		onClose();
		switch (action) {
			case 'new-chat':
				onCreateChat();
				break;
			case 'new-agent':
				onCreateAgent();
				break;
			case 'terminal':
				onCreateTerminal();
				break;
			case 'image-studio':
				onOpenImageStudio?.();
				break;
			case 'model-studio':
				onOpenModelStudio?.();
				break;
			case 'audio-studio':
				onOpenAudioStudio?.();
				break;
			case 'file-browser':
				onOpenFileBrowser?.();
				break;
			case 'projects':
				onOpenProjects?.();
				break;
			case 'profiles':
				onOpenProfiles?.();
				break;
			case 'settings':
				onOpenSettings?.();
				break;
		}
	}

	// Navigate to category (sub-deck)
	function navigateToCategory(card: NavigatorCard) {
		if (card.category === 'recent') {
			currentView = 'recent';
			navStack = [...navStack, { id: 'recent', name: 'Recent', type: 'category' }];
		}

		// Reset scroll
		if (carouselRef) {
			carouselRef.scrollLeft = 0;
		}
	}

	// Navigate back via breadcrumb
	function navigateBack(targetIndex: number) {
		if (targetIndex >= navStack.length - 1) return;

		navStack = navStack.slice(0, targetIndex + 1);
		const target = navStack[targetIndex];

		if (target.type === 'root') {
			currentView = 'main';
		} else if (target.id === 'recent') {
			currentView = 'recent';
		}

		// Reset scroll
		if (carouselRef) {
			carouselRef.scrollLeft = 0;
		}
	}

	// Reset state when closing
	function handleClose() {
		onClose();
		// Delay reset to allow exit animation
		setTimeout(() => {
			navStack = [{ id: 'root', name: 'AI Shuffle', type: 'root' }];
			currentView = 'main';
			scrollProgress = 0;
		}, 300);
	}

	// Handle keyboard
	function handleKeyDown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			if (navStack.length > 1) {
				navigateBack(navStack.length - 2);
			} else {
				handleClose();
			}
		}
	}

	// Drag to scroll handlers
	function handleMouseDown(e: MouseEvent) {
		if (!carouselRef) return;
		isDragging = true;
		hasDragged = false; // Reset drag state
		startX = e.pageX - carouselRef.offsetLeft;
		scrollLeft = carouselRef.scrollLeft;
	}

	function handleMouseMove(e: MouseEvent) {
		if (!isDragging || !carouselRef) return;
		const x = e.pageX - carouselRef.offsetLeft;
		const walk = (x - startX) * 1.5;

		// Only mark as dragged if moved beyond threshold
		if (Math.abs(x - startX) > DRAG_THRESHOLD) {
			hasDragged = true;
			e.preventDefault();
		}

		carouselRef.scrollLeft = scrollLeft - walk;
	}

	function handleMouseUp() {
		isDragging = false;
		// Reset hasDragged after a short delay to allow click events to check it
		setTimeout(() => {
			hasDragged = false;
		}, 50);
	}

	function handleMouseLeave() {
		isDragging = false;
		hasDragged = false;
	}

	// Touch handlers for mobile
	function handleTouchStart(e: TouchEvent) {
		if (!carouselRef) return;
		isDragging = true;
		hasDragged = false;
		startX = e.touches[0].pageX - carouselRef.offsetLeft;
		scrollLeft = carouselRef.scrollLeft;
	}

	function handleTouchMove(e: TouchEvent) {
		if (!isDragging || !carouselRef) return;
		const x = e.touches[0].pageX - carouselRef.offsetLeft;
		const walk = (x - startX) * 1.5;

		// Only mark as dragged if moved beyond threshold
		if (Math.abs(x - startX) > DRAG_THRESHOLD) {
			hasDragged = true;
		}

		carouselRef.scrollLeft = scrollLeft - walk;
	}

	function handleTouchEnd() {
		isDragging = false;
		setTimeout(() => {
			hasDragged = false;
		}, 50);
	}

	// Handle mouse wheel for horizontal scrolling
	function handleWheel(e: WheelEvent) {
		if (!carouselRef) return;

		// Prevent default vertical scroll and convert to horizontal
		e.preventDefault();

		// Use deltaY for horizontal scrolling (more natural with trackpads/wheels)
		// Multiply by 3 for faster scrolling
		const scrollAmount = (e.deltaY !== 0 ? e.deltaY : e.deltaX) * 3;
		const newScrollLeft = carouselRef.scrollLeft + scrollAmount;

		// Clamp to valid range
		const maxScroll = carouselRef.scrollWidth - carouselRef.clientWidth;
		carouselRef.scrollLeft = Math.max(0, Math.min(newScrollLeft, maxScroll));
	}

	// Update scroll progress for dots
	function handleScroll() {
		if (!carouselRef) return;
		const maxScroll = carouselRef.scrollWidth - carouselRef.clientWidth;
		scrollProgress = maxScroll > 0 ? carouselRef.scrollLeft / maxScroll : 0;
	}

	// Calculate number of dots based on cards
	const dotCount = $derived(Math.min(5, Math.ceil(currentCards.length / 3)));
	const activeDot = $derived(Math.round(scrollProgress * (dotCount - 1)));
</script>

<svelte:window onkeydown={handleKeyDown} />

{#if open}
	<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
	<div
		class="navigator"
		class:open
		transition:fade={{ duration: 200 }}
	>
		<!-- Backdrop -->
		<div class="navigator-backdrop" onclick={handleClose}></div>

		<!-- Header with breadcrumb -->
		<header class="navigator-header">
			<nav class="nav-breadcrumb">
				{#each navStack as item, index}
					{#if index > 0}
						<span class="breadcrumb-sep">/</span>
					{/if}
					<button
						class="breadcrumb-btn"
						class:active={index === navStack.length - 1}
						disabled={index === navStack.length - 1}
						onclick={() => navigateBack(index)}
					>
						{item.name}
					</button>
				{/each}
			</nav>
			<button class="nav-close" onclick={handleClose} aria-label="Close">
				<X size={18} strokeWidth={2} />
			</button>
		</header>

		<!-- Card carousel -->
		<div class="carousel-stage">
			{#key currentView}
				<div
					class="carousel"
					class:dragging={isDragging}
					bind:this={carouselRef}
					onmousedown={handleMouseDown}
					onmousemove={handleMouseMove}
					onmouseup={handleMouseUp}
					onmouseleave={handleMouseLeave}
					ontouchstart={handleTouchStart}
					ontouchmove={handleTouchMove}
					ontouchend={handleTouchEnd}
					onwheel={handleWheel}
					onscroll={handleScroll}
					role="listbox"
					aria-label="Card navigator"
					in:fly={{ x: 100, duration: 300, easing: cubicOut }}
					out:fly={{ x: -100, duration: 200, easing: cubicOut }}
				>
					{#each currentCards as card, index (card.id)}
						<!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
						<article
							class="card"
							class:thread-card={card.type === 'thread'}
							data-ai-active={card.isStreaming}
							data-has-children={card.hasChildren}
							style:animation-delay="{index * 50}ms"
							onclick={() => handleCardClick(card)}
							onkeydown={(e) => e.key === 'Enter' && handleCardClick(card)}
							role="option"
							tabindex="0"
							aria-selected="false"
						>
						<div class="card-inner">
							{#if card.isStreaming}
								<div class="card-status">Streaming</div>
							{/if}

							<div class="card-icon">
								<card.icon size={20} strokeWidth={1.75} />
							</div>

							<h3 class="card-title">{card.title}</h3>

							{#if card.type === 'thread'}
								<div class="card-meta">
									{#if card.lastActive}
										<span class="card-meta-item">
											<Clock size={12} />
											{card.lastActive}
										</span>
									{/if}
									{#if card.messageCount}
										<span class="card-meta-item">
											<MessageSquare size={12} />
											{card.messageCount}
										</span>
									{/if}
								</div>
							{:else if card.subtitle}
								<p class="card-subtitle">{card.subtitle}</p>
							{/if}
						</div>
					</article>
				{/each}

				{#if currentCards.length === 0}
					<div class="empty-state">
						<p>No items to show</p>
					</div>
				{/if}
				</div>
			{/key}

			<!-- Scroll hint with dots -->
			{#if currentCards.length > 3}
				<div class="scroll-hint">
					<span class="scroll-hint-text">{isMobile ? 'Swipe' : 'Scroll'} to browse</span>
					<div class="scroll-dots">
						{#each Array(dotCount) as _, i}
							<span class="scroll-dot" class:active={i === activeDot}></span>
						{/each}
					</div>
				</div>
			{/if}
		</div>
	</div>
{/if}

<style>
	/* ========================================
	   AI SHUFFLE DESIGN SYSTEM
	   Clean, modern AI workspace aesthetic
	   ======================================== */

	/* Base Colors - using CSS variables from app.css with fallbacks */
	.navigator {
		--bg-deep: oklch(0.08 0.01 260);
		--bg-base: oklch(0.10 0.01 260);
		--bg-elevated: oklch(0.13 0.01 260);
		--bg-card: oklch(0.15 0.01 260);
		--bg-card-hover: oklch(0.17 0.01 260);

		--border-subtle: rgba(255, 255, 255, 0.06);
		--border-default: rgba(255, 255, 255, 0.1);
		--border-strong: rgba(255, 255, 255, 0.15);

		--text-primary: #f4f4f5;
		--text-secondary: #a1a1aa;
		--text-muted: #71717a;
		--text-dim: #52525b;

		--ai-primary: #22d3ee;
		--ai-secondary: #06b6d4;
		--ai-glow: rgba(34, 211, 238, 0.25);
		--ai-subtle: rgba(34, 211, 238, 0.08);

		--ease-out: cubic-bezier(0.16, 1, 0.3, 1);
		--ease-spring: cubic-bezier(0.34, 1.56, 0.64, 1);

		--card-width: 220px;
		--card-height: 160px;
		--card-gap: 16px;
		--radius-sm: 8px;
		--radius-md: 12px;
		--radius-lg: 16px;
	}

	/* ========================================
	   NAVIGATOR OVERLAY
	   ======================================== */
	.navigator {
		position: fixed;
		inset: 0;
		z-index: 100000;
		display: flex;
		flex-direction: column;
	}

	.navigator-backdrop {
		position: absolute;
		inset: 0;
		background: rgba(0, 0, 0, 0.85);
		backdrop-filter: blur(20px);
		-webkit-backdrop-filter: blur(20px);
	}

	/* ========================================
	   HEADER
	   ======================================== */
	.navigator-header {
		position: relative;
		z-index: 10;
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 20px 24px;
		padding-top: max(20px, env(safe-area-inset-top, 20px));
	}

	.nav-breadcrumb {
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.breadcrumb-btn {
		font-family: inherit;
		font-size: 0.9375rem;
		font-weight: 500;
		color: var(--text-muted);
		background: none;
		border: none;
		cursor: pointer;
		padding: 6px 10px;
		border-radius: var(--radius-sm);
		transition: all 0.15s ease;
	}

	.breadcrumb-btn:hover:not(.active):not(:disabled) {
		color: var(--text-secondary);
		background: var(--border-subtle);
	}

	.breadcrumb-btn.active {
		color: var(--text-primary);
		cursor: default;
	}

	.breadcrumb-sep {
		color: var(--text-dim);
		font-size: 0.75rem;
	}

	.nav-close {
		width: 40px;
		height: 40px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--bg-elevated);
		border: 1px solid var(--border-default);
		border-radius: 50%;
		color: var(--text-secondary);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.nav-close:hover {
		background: var(--bg-card);
		border-color: var(--border-strong);
		color: var(--text-primary);
	}

	/* ========================================
	   CARD CAROUSEL
	   ======================================== */
	.carousel-stage {
		flex: 1;
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		overflow: hidden;
		width: 100%;
		min-height: 0; /* Important for flexbox */
	}

	.carousel {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		gap: var(--card-gap);
		padding: 40px 60px;
		overflow-x: scroll;
		overflow-y: hidden;
		/* Removed scroll-snap - was interfering with programmatic scrolling */
		scrollbar-width: none;
		cursor: grab;
		-webkit-overflow-scrolling: touch; /* Smooth scrolling on iOS */
	}

	.carousel::-webkit-scrollbar {
		display: none;
	}

	.carousel:active,
	.carousel.dragging {
		cursor: grabbing;
		scroll-behavior: auto;
	}

	/* Edge fade */
	.carousel-stage::before,
	.carousel-stage::after {
		content: '';
		position: absolute;
		top: 0;
		bottom: 0;
		width: 100px;
		z-index: 10;
		pointer-events: none;
	}

	.carousel-stage::before {
		left: 0;
		background: linear-gradient(90deg, var(--bg-deep) 0%, transparent 100%);
	}

	.carousel-stage::after {
		right: 0;
		background: linear-gradient(-90deg, var(--bg-deep) 0%, transparent 100%);
	}

	/* ========================================
	   CARDS
	   ======================================== */
	.card {
		flex-shrink: 0;
		width: var(--card-width);
		height: var(--card-height);
		cursor: pointer;
		transition: transform 0.2s var(--ease-out);

		/* Staggered entrance animation - delay set via inline style */
		opacity: 0;
		transform: translateX(40px) scale(0.95);
		animation: cardEnter 0.4s var(--ease-spring) forwards;
	}

	@keyframes cardEnter {
		0% {
			opacity: 0;
			transform: translateX(40px) scale(0.95);
		}
		100% {
			opacity: 1;
			transform: translateX(0) scale(1);
		}
	}

	.card:hover {
		transform: translateY(-6px);
	}

	.card:active {
		transform: translateY(-4px) scale(0.98);
	}

	.card-inner {
		width: 100%;
		height: 100%;
		background: var(--bg-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-lg);
		padding: 20px;
		display: flex;
		flex-direction: column;
		position: relative;
		overflow: hidden;
		transition: all 0.2s var(--ease-out);
	}

	.card:hover .card-inner {
		background: var(--bg-card-hover);
		border-color: var(--border-strong);
		box-shadow:
			0 8px 32px rgba(0, 0, 0, 0.4),
			0 0 0 1px var(--border-subtle);
	}

	/* AI-active card glow */
	.card[data-ai-active="true"] .card-inner {
		border-color: rgba(34, 211, 238, 0.3);
		box-shadow:
			0 0 20px var(--ai-glow),
			0 8px 32px rgba(0, 0, 0, 0.3);
	}

	/* Card icon */
	.card-icon {
		width: 40px;
		height: 40px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--bg-elevated);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md);
		margin-bottom: 16px;
		color: var(--text-secondary);
		transition: all 0.2s ease;
	}

	.card:hover .card-icon {
		background: var(--ai-subtle);
		border-color: rgba(34, 211, 238, 0.2);
		color: var(--ai-primary);
	}

	.card[data-ai-active="true"] .card-icon {
		background: var(--ai-subtle);
		border-color: rgba(34, 211, 238, 0.3);
		color: var(--ai-primary);
	}

	.card[data-ai-active="true"] .card-icon :global(svg) {
		animation: pulse 2s ease-in-out infinite;
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.5; }
	}

	/* Card content */
	.card-title {
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--text-primary);
		margin-bottom: 4px;
		line-height: 1.3;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	.card-subtitle {
		font-size: 0.8125rem;
		color: var(--text-muted);
		line-height: 1.4;
	}

	/* Card meta (for thread cards) */
	.card-meta {
		margin-top: auto;
		display: flex;
		align-items: center;
		gap: 12px;
		font-size: 0.75rem;
		color: var(--text-dim);
	}

	.card-meta-item {
		display: flex;
		align-items: center;
		gap: 4px;
	}

	/* Category indicator - chevron */
	.card[data-has-children="true"] .card-inner::after {
		content: '\203A';
		position: absolute;
		right: 16px;
		top: 50%;
		transform: translateY(-50%);
		font-size: 1.25rem;
		font-weight: 300;
		color: var(--text-dim);
		opacity: 0.6;
		transition: all 0.2s ease;
	}

	.card[data-has-children="true"]:hover .card-inner::after {
		opacity: 1;
		color: var(--ai-primary);
		transform: translateY(-50%) translateX(2px);
	}

	/* Status badge */
	.card-status {
		position: absolute;
		top: 12px;
		right: 12px;
		padding: 3px 8px;
		background: var(--ai-subtle);
		border: 1px solid rgba(34, 211, 238, 0.2);
		border-radius: 4px;
		font-family: ui-monospace, monospace;
		font-size: 0.625rem;
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--ai-primary);
		display: flex;
		align-items: center;
		gap: 4px;
	}

	.card-status::before {
		content: '';
		width: 5px;
		height: 5px;
		background: var(--ai-primary);
		border-radius: 50%;
		animation: blink 1.5s ease-in-out infinite;
	}

	@keyframes blink {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.3; }
	}

	/* Thread card variant */
	.card.thread-card {
		height: 180px;
	}

	/* Empty state */
	.empty-state {
		width: 100%;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 40px;
		color: var(--text-muted);
		font-size: 0.9375rem;
	}

	/* ========================================
	   SCROLL INDICATOR
	   ======================================== */
	.scroll-hint {
		position: absolute;
		bottom: 28px;
		left: 50%;
		transform: translateX(-50%);
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 18px;
		background: var(--bg-elevated);
		border: 1px solid var(--border-subtle);
		border-radius: 20px;
		z-index: 20;
	}

	.scroll-hint-text {
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--text-muted);
	}

	.scroll-dots {
		display: flex;
		gap: 5px;
	}

	.scroll-dot {
		width: 5px;
		height: 5px;
		border-radius: 50%;
		background: var(--text-dim);
		transition: all 0.2s ease;
	}

	.scroll-dot.active {
		width: 16px;
		border-radius: 3px;
		background: var(--ai-primary);
	}

	/* ========================================
	   MOBILE RESPONSIVE
	   ======================================== */
	@media (max-width: 640px) {
		.navigator {
			--card-width: 180px;
			--card-height: 140px;
			--card-gap: 12px;
		}

		.navigator-header {
			padding: 16px 20px;
		}

		.breadcrumb-btn {
			font-size: 0.875rem;
			padding: 4px 8px;
		}

		.carousel {
			padding: 30px 40px;
		}

		.carousel-stage::before,
		.carousel-stage::after {
			width: 60px;
		}

		.card-inner {
			padding: 16px;
		}

		.card-icon {
			width: 36px;
			height: 36px;
			margin-bottom: 12px;
		}

		.card-title {
			font-size: 0.875rem;
		}

		.card-subtitle {
			font-size: 0.75rem;
		}

		.card.thread-card {
			height: 160px;
		}

		.scroll-hint {
			bottom: max(100px, calc(env(safe-area-inset-bottom) + 80px));
		}
	}

	/* Large screens */
	@media (min-width: 1200px) {
		.navigator {
			--card-width: 260px;
			--card-height: 180px;
			--card-gap: 20px;
		}

		.card.thread-card {
			height: 200px;
		}
	}

	/* ========================================
	   REDUCED MOTION
	   ======================================== */
	@media (prefers-reduced-motion: reduce) {
		*, *::before, *::after {
			animation-duration: 0.01ms !important;
			animation-iteration-count: 1 !important;
			transition-duration: 0.01ms !important;
		}
	}
</style>
