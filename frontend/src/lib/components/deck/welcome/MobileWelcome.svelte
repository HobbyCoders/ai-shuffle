<script lang="ts">
	/**
	 * MobileWelcome - Mobile-optimized welcome screen with horizontal carousel
	 *
	 * Features:
	 * - Horizontal swipeable carousel of welcome cards
	 * - Touch-friendly with momentum scrolling
	 * - Dot indicators for position
	 * - Same cards as desktop WelcomeCards but optimized for mobile
	 */

	import { MessageSquare, Terminal, FolderOpen, User, Bot } from 'lucide-svelte';
	import WelcomeTitle from './WelcomeTitle.svelte';
	import SpotlightEffect from './SpotlightEffect.svelte';

	interface Props {
		onCreateCard: (type: string) => void;
	}

	let { onCreateCard }: Props = $props();

	// Card definitions - same as WelcomeCards.svelte
	const cards = [
		{
			type: 'chat',
			label: 'CHAT',
			description: 'Start a conversation',
			icon: MessageSquare,
		},
		{
			type: 'terminal',
			label: 'TERMINAL',
			description: 'Open command line',
			icon: Terminal,
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

	// Carousel state
	let currentIndex = $state(0);
	let carouselEl: HTMLDivElement | undefined = $state();
	let touchStartX = $state(0);
	let touchDeltaX = $state(0);
	let isDragging = $state(false);

	// Navigate to specific card
	function goToCard(index: number) {
		currentIndex = Math.max(0, Math.min(index, cards.length - 1));
		scrollToCard(currentIndex);
	}

	function scrollToCard(index: number) {
		if (!carouselEl) return;
		const cardWidth = carouselEl.offsetWidth * 0.75; // Card is 75% of viewport
		const gap = 16;
		const offset = index * (cardWidth + gap);
		carouselEl.scrollTo({ left: offset, behavior: 'smooth' });
	}

	// Touch handlers
	function handleTouchStart(e: TouchEvent) {
		touchStartX = e.touches[0].clientX;
		touchDeltaX = 0;
		isDragging = true;
	}

	function handleTouchMove(e: TouchEvent) {
		if (!isDragging) return;
		touchDeltaX = e.touches[0].clientX - touchStartX;
	}

	function handleTouchEnd() {
		if (!isDragging) return;
		isDragging = false;

		const threshold = 50;
		if (touchDeltaX < -threshold && currentIndex < cards.length - 1) {
			goToCard(currentIndex + 1);
		} else if (touchDeltaX > threshold && currentIndex > 0) {
			goToCard(currentIndex - 1);
		} else {
			scrollToCard(currentIndex);
		}
		touchDeltaX = 0;
	}

	// Handle scroll snap detection
	function handleScroll() {
		if (!carouselEl || isDragging) return;
		const cardWidth = carouselEl.offsetWidth * 0.75;
		const gap = 16;
		const scrollPos = carouselEl.scrollLeft;
		const newIndex = Math.round(scrollPos / (cardWidth + gap));
		if (newIndex !== currentIndex && newIndex >= 0 && newIndex < cards.length) {
			currentIndex = newIndex;
		}
	}
</script>

<div class="mobile-welcome">
	<SpotlightEffect />

	<!-- Subtle grid pattern background -->
	<div class="grid-pattern" aria-hidden="true"></div>

	<div class="welcome-content">
		<WelcomeTitle />

		<p class="welcome-tagline">Your AI workspace awaits</p>

		<!-- Horizontal Carousel -->
		<div
			bind:this={carouselEl}
			class="carousel"
			ontouchstart={handleTouchStart}
			ontouchmove={handleTouchMove}
			ontouchend={handleTouchEnd}
			onscroll={handleScroll}
		>
			{#each cards as card, i}
				<button
					class="carousel-card"
					class:active={i === currentIndex}
					onclick={() => onCreateCard(card.type)}
				>
					<div class="card-icon">
						<card.icon size={32} strokeWidth={1.5} />
					</div>
					<span class="card-label">{card.label}</span>
					<span class="card-description">{card.description}</span>
				</button>
			{/each}
		</div>

		<!-- Dot Indicators -->
		<div class="dots">
			{#each cards as _, i}
				<button
					class="dot"
					class:active={i === currentIndex}
					onclick={() => goToCard(i)}
					aria-label="Go to card {i + 1}"
				></button>
			{/each}
		</div>

		<!-- Swipe hint -->
		<p class="swipe-hint">Swipe to explore</p>
	</div>
</div>

<style>
	.mobile-welcome {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;

		/* Dark workspace gradient - subtle depth */
		background:
			radial-gradient(
				ellipse 80% 60% at 50% 40%,
				oklch(0.18 0.01 260) 0%,
				oklch(0.14 0.008 260) 60%,
				oklch(0.12 0.006 260) 100%
			);

		overflow: hidden;
	}

	/* Subtle grid pattern overlay - tech/workspace feel */
	.grid-pattern {
		position: absolute;
		inset: 0;
		background-image:
			linear-gradient(rgba(255, 255, 255, 0.02) 1px, transparent 1px),
			linear-gradient(90deg, rgba(255, 255, 255, 0.02) 1px, transparent 1px);
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
		scroll-snap-type: x mandatory;
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

		background: oklch(0.17 0.01 260);
		border: 1px solid rgba(255, 255, 255, 0.08);
		border-radius: 20px;

		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 0.75rem;

		cursor: pointer;
		scroll-snap-align: center;
		transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
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
		border-color: var(--accent-cyan, #22d3ee);
		box-shadow:
			0 12px 32px rgba(0, 0, 0, 0.5),
			0 0 24px rgba(34, 211, 238, 0.15);
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
		border-radius: 16px;
		background: rgba(255, 255, 255, 0.04);
		border: 1px solid rgba(255, 255, 255, 0.06);
		color: var(--muted-foreground);
		transition: all 0.25s ease-out;
	}

	.carousel-card.active .card-icon {
		background: rgba(34, 211, 238, 0.1);
		border-color: var(--accent-cyan, #22d3ee);
		color: var(--accent-cyan, #22d3ee);
		box-shadow: 0 0 20px rgba(34, 211, 238, 0.2);
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
		background: rgba(255, 255, 255, 0.2);
		border: none;
		padding: 0;
		cursor: pointer;
		transition: all 0.2s ease;
		-webkit-tap-highlight-color: transparent;
	}

	.dot.active {
		background: var(--accent-cyan, #22d3ee);
		box-shadow: 0 0 8px rgba(34, 211, 238, 0.4);
	}

	.dot:not(.active):hover {
		background: rgba(255, 255, 255, 0.4);
	}

	/* Swipe Hint */
	.swipe-hint {
		font-size: 0.7rem;
		color: var(--muted-foreground);
		opacity: 0.6;
		margin-top: 0.25rem;
		animation: fadeInUp 0.6s ease-out 1s forwards;
		opacity: 0;
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

		.carousel-card {
			transform: none;
		}
	}
</style>
