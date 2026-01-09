<script lang="ts">
	/**
	 * MobileWelcome - Mobile-optimized welcome screen with horizontal carousel
	 *
	 * Features:
	 * - Horizontal swipeable carousel of welcome cards
	 * - Touch-friendly with momentum scrolling
	 * - Dot indicators for position
	 * - Same cards as desktop WelcomeCards but optimized for mobile
	 * - Full accessibility support with ARIA attributes
	 */

	import { MessageSquare, History, FolderOpen, User, Bot, Key, Files } from 'lucide-svelte';
	import WelcomeTitle from './WelcomeTitle.svelte';
	import SpotlightEffect from './SpotlightEffect.svelte';

	interface Props {
		onCreateCard: (type: string) => void;
		isApiUser?: boolean;
	}

	let { onCreateCard, isApiUser = false }: Props = $props();

	// Carousel layout constants - single source of truth
	const CARD_WIDTH_PERCENT = 0.75; // Card is 75% of viewport
	const CARD_GAP = 16; // Gap between cards in pixels

	// Card type definition
	type CardDef = {
		type: string;
		label: string;
		description: string;
		icon: typeof MessageSquare;
	};

	// Admin card definitions
	const adminCards: CardDef[] = [
		{
			type: 'chat',
			label: 'CHAT',
			description: 'Start a conversation',
			icon: MessageSquare,
		},
		{
			type: 'recent-sessions',
			label: 'RECENT',
			description: 'Recent conversations',
			icon: History,
		},
		{
			type: 'project',
			label: 'PROJECTS',
			description: 'Manage workspaces',
			icon: FolderOpen,
		},
		{
			type: 'profile',
			label: 'PROFILES',
			description: 'Configure agents',
			icon: User,
		},
		{
			type: 'subagent',
			label: 'SUBAGENTS',
			description: 'Specialized assistants',
			icon: Bot,
		}
	];

	// API user card definitions (limited set)
	const apiUserCards: CardDef[] = [
		{
			type: 'chat',
			label: 'CHAT',
			description: 'Start a conversation',
			icon: MessageSquare,
		},
		{
			type: 'recent-sessions',
			label: 'RECENT',
			description: 'Recent conversations',
			icon: History,
		},
		{
			type: 'file-browser',
			label: 'FILES',
			description: 'Browse project files',
			icon: Files,
		},
		{
			type: 'user-settings',
			label: 'SETTINGS',
			description: 'API keys & profile',
			icon: Key,
		}
	];

	// Select cards based on user type
	const cards = $derived(isApiUser ? apiUserCards : adminCards);

	// Carousel state - relies entirely on CSS scroll-snap for smooth swiping
	let currentIndex = $state(0);
	let carouselEl: HTMLDivElement | undefined = $state();
	let rafId: number | null = null; // For real-time scroll tracking

	// Helper to calculate card dimensions
	function getCardMetrics() {
		if (!carouselEl) return { cardWidth: 0, gap: CARD_GAP };
		return {
			cardWidth: carouselEl.offsetWidth * CARD_WIDTH_PERCENT,
			gap: CARD_GAP
		};
	}

	// Navigate to specific card (for dot indicator clicks)
	function goToCard(index: number) {
		const clampedIndex = Math.max(0, Math.min(index, cards.length - 1));
		if (clampedIndex === currentIndex || !carouselEl) return;

		currentIndex = clampedIndex;
		const { cardWidth, gap } = getCardMetrics();
		const offset = index * (cardWidth + gap);
		carouselEl.scrollTo({ left: offset, behavior: 'smooth' });
	}

	// Find closest card to center - extracted for reuse
	function findClosestCardIndex(): number {
		if (!carouselEl) return currentIndex;

		const carouselRect = carouselEl.getBoundingClientRect();
		const carouselCenter = carouselRect.left + carouselRect.width / 2;
		const cardElements = carouselEl.querySelectorAll('.carousel-card');

		let closestIndex = 0;
		let closestDistance = Infinity;

		cardElements.forEach((card, index) => {
			const cardRect = card.getBoundingClientRect();
			const cardCenter = cardRect.left + cardRect.width / 2;
			const distance = Math.abs(carouselCenter - cardCenter);

			if (distance < closestDistance) {
				closestDistance = distance;
				closestIndex = index;
			}
		});

		return closestIndex;
	}

	// Handle scroll with real-time tracking using RAF for smooth visual updates
	function handleScroll() {
		if (!carouselEl) return;

		// Cancel any pending RAF to prevent stacking
		if (rafId !== null) {
			cancelAnimationFrame(rafId);
		}

		// Use RAF for smooth, immediate visual feedback
		rafId = requestAnimationFrame(() => {
			const closestIndex = findClosestCardIndex();
			if (closestIndex !== currentIndex) {
				currentIndex = closestIndex;
			}
			rafId = null;
		});
	}
</script>

<div class="mobile-welcome">
	<SpotlightEffect />

	<!-- Subtle grid pattern background -->
	<div class="grid-pattern" aria-hidden="true"></div>

	<div class="welcome-content">
		<WelcomeTitle />

		<p class="welcome-tagline">Your AI workspace awaits</p>

		<!-- Horizontal Carousel with ARIA support -->
		<!-- Uses CSS scroll-snap for smooth native swiping behavior -->
		<div
			bind:this={carouselEl}
			class="carousel"
			role="region"
			aria-label="Welcome cards carousel"
			aria-roledescription="carousel"
			onscroll={handleScroll}
		>
			{#each cards as card, i}
				<button
					class="carousel-card"
					class:active={i === currentIndex}
					onclick={() => onCreateCard(card.type)}
					aria-current={i === currentIndex ? 'true' : undefined}
					aria-label="{card.label}: {card.description}"
				>
					<div class="card-icon">
						<card.icon size={32} strokeWidth={1.5} />
					</div>
					<span class="card-label">{card.label}</span>
					<span class="card-description">{card.description}</span>
				</button>
			{/each}
		</div>

		<!-- Dot Indicators with proper ARIA -->
		<div class="dots" role="tablist" aria-label="Carousel navigation">
			{#each cards as card, i}
				<button
					class="dot"
					class:active={i === currentIndex}
					onclick={() => goToCard(i)}
					role="tab"
					aria-selected={i === currentIndex}
					aria-label="Go to {card.label} card"
				></button>
			{/each}
		</div>

		<!-- Swipe hint -->
		<p class="swipe-hint" aria-hidden="true">Swipe to explore</p>
	</div>
</div>

<style>
	/* CSS Custom Properties for consistent theming */
	.mobile-welcome {

		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;

		/* Use theme-aware background gradient (matches WelcomeHero) */
		background:
			radial-gradient(
				ellipse 80% 60% at 50% 40%,
				color-mix(in oklch, var(--card) 100%, var(--background)) 0%,
				var(--background) 60%,
				color-mix(in oklch, var(--background) 95%, black) 100%
			);

		overflow: hidden;
	}

	/* Subtle grid pattern overlay - tech/workspace feel (matches WelcomeHero) */
	.grid-pattern {
		position: absolute;
		inset: 0;
		/* Use theme-aware grid color */
		background-image:
			linear-gradient(color-mix(in oklch, var(--border) 20%, transparent) 1px, transparent 1px),
			linear-gradient(90deg, color-mix(in oklch, var(--border) 20%, transparent) 1px, transparent 1px);
		background-size: 30px 30px;
		opacity: 0.5;
		pointer-events: none;
	}

	/* Soft vignette effect */
	.mobile-welcome::after {
		content: '';
		position: absolute;
		inset: 0;
		background: radial-gradient(ellipse 90% 90% at 50% 50%, transparent 40%, rgba(0, 0, 0, 0.3) 100%);
		pointer-events: none;
	}

	.welcome-content {
		position: relative;
		z-index: 10;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 1.25rem;
		padding: 1.5rem;
		width: 100%;
		max-width: 100vw;
		overflow: hidden;
	}

	.welcome-tagline {
		font-family: var(--font-sans, system-ui);
		font-size: 0.75rem;
		font-weight: 400;
		color: var(--muted-foreground);
		letter-spacing: 0.15em;
		text-transform: uppercase;
		opacity: 0;
		animation: fadeInUp 0.6s ease-out 0.5s forwards;
	}

	/* Carousel Container */
	.carousel {
		display: flex;
		gap: 16px;
		width: 100%;
		overflow-x: auto;
		scroll-snap-type: x proximity; /* proximity = lighter feel, only snaps when close */
		scrollbar-width: none;
		-ms-overflow-style: none;
		padding: 1rem 12.5%; /* Center first/last cards */
		-webkit-overflow-scrolling: touch;
	}

	.carousel::-webkit-scrollbar {
		display: none;
	}

	/* Carousel Card */
	.carousel-card {
		flex-shrink: 0;
		width: 75%;
		min-width: 200px;
		max-width: 280px;
		aspect-ratio: 3 / 4;
		padding: 1.5rem 1rem;

		/* Use theme-aware card background */
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius-lg, 20px);

		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 0.75rem;

		cursor: pointer;
		scroll-snap-align: center;
		/* scroll-snap-stop: normal allows momentum to carry through cards naturally */
		/* Split transitions: fast for glow/border (scroll response), smooth for transform (tap) */
		transition:
			border-color 0.1s ease-out,
			box-shadow 0.15s ease-out,
			transform 0.2s cubic-bezier(0.16, 1, 0.3, 1);
		-webkit-tap-highlight-color: transparent;

		box-shadow:
			0 8px 24px rgba(0, 0, 0, 0.4),
			0 4px 8px rgba(0, 0, 0, 0.3);

		opacity: 0;
		animation: fadeInScale 0.5s ease-out forwards;
		animation-delay: calc(0.6s + var(--i, 0) * 0.1s);
	}

	.carousel-card:nth-child(1) { --i: 0; }
	.carousel-card:nth-child(2) { --i: 1; }
	.carousel-card:nth-child(3) { --i: 2; }
	.carousel-card:nth-child(4) { --i: 3; }
	.carousel-card:nth-child(5) { --i: 4; }

	.carousel-card.active {
		border-color: var(--primary);
		box-shadow:
			0 12px 32px rgba(0, 0, 0, 0.5),
			0 0 24px var(--glow-color-soft);
	}

	.carousel-card:active {
		transform: scale(0.97);
	}

	/* Card Content */
	.card-icon {
		width: 64px;
		height: 64px;
		display: flex;
		align-items: center;
		justify-content: center;
		border-radius: var(--radius, 16px);
		background: color-mix(in oklch, var(--foreground) 4%, transparent);
		border: 1px solid color-mix(in oklch, var(--foreground) 6%, transparent);
		color: var(--muted-foreground);
		transition: all 0.12s ease-out; /* Fast response to scroll */
	}

	.carousel-card.active .card-icon {
		background: var(--glow-color-soft);
		border-color: var(--primary);
		color: var(--primary);
		box-shadow: 0 0 20px var(--glow-color-soft);
	}

	.card-label {
		font-family: var(--font-sans, system-ui);
		font-size: 0.9rem;
		font-weight: 600;
		letter-spacing: 0.1em;
		color: var(--foreground);
		text-transform: uppercase;
	}

	.card-description {
		font-size: 0.8rem;
		color: var(--muted-foreground);
		text-align: center;
		line-height: 1.4;
	}

	/* Dot Indicators */
	.dots {
		display: flex;
		gap: 8px;
		margin-top: 0.5rem;
	}

	.dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: color-mix(in oklch, var(--foreground) 20%, transparent);
		border: none;
		padding: 0;
		cursor: pointer;
		transition: all 0.1s ease-out; /* Fast response to scroll */
		-webkit-tap-highlight-color: transparent;
	}

	.dot.active {
		background: var(--primary);
		box-shadow: 0 0 8px color-mix(in oklch, var(--primary) 40%, transparent);
	}

	.dot:not(.active):hover {
		background: color-mix(in oklch, var(--foreground) 40%, transparent);
	}

	/* Swipe Hint - fixed opacity animation */
	.swipe-hint {
		font-size: 0.7rem;
		color: var(--muted-foreground);
		margin-top: 0.25rem;
		opacity: 0;
		animation: fadeInHint 0.6s ease-out 1s forwards;
	}

	/* Animations */
	@keyframes fadeInUp {
		from {
			opacity: 0;
			transform: translateY(10px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	@keyframes fadeInHint {
		from {
			opacity: 0;
			transform: translateY(10px);
		}
		to {
			opacity: 0.6;
			transform: translateY(0);
		}
	}

	@keyframes fadeInScale {
		from {
			opacity: 0;
			transform: scale(0.9);
		}
		to {
			opacity: 1;
			transform: scale(1);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.welcome-tagline,
		.carousel-card,
		.swipe-hint {
			animation: none;
			opacity: 1;
		}

		.swipe-hint {
			opacity: 0.6;
		}

		.carousel-card {
			transform: none;
		}
	}
</style>
