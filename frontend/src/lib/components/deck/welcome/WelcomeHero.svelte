<script lang="ts">
	import WelcomeTitle from './WelcomeTitle.svelte';
	import WelcomeCards from './WelcomeCards.svelte';
	import SpotlightEffect from './SpotlightEffect.svelte';

	interface Props {
		onCreateCard: (type: string) => void;
		isApiUser?: boolean;
	}

	let { onCreateCard, isApiUser = false }: Props = $props();
</script>

<div class="welcome-hero">
	<SpotlightEffect />

	<!-- Subtle grid pattern background -->
	<div class="grid-pattern" aria-hidden="true"></div>

	<div class="welcome-content">
		<WelcomeTitle />

		<p class="welcome-tagline">Your AI workspace awaits</p>

		<WelcomeCards {onCreateCard} {isApiUser} />
	</div>
</div>

<style>
	.welcome-hero {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;

		/* Use theme-aware background gradient */
		background:
			radial-gradient(
				ellipse 80% 60% at 50% 40%,
				color-mix(in oklch, var(--card) 100%, var(--background)) 0%,
				var(--background) 60%,
				color-mix(in oklch, var(--background) 95%, black) 100%
			);

		overflow: hidden;
	}

	/* Subtle grid pattern overlay - tech/workspace feel */
	.grid-pattern {
		position: absolute;
		inset: 0;
		/* Use theme-aware grid color */
		background-image:
			linear-gradient(color-mix(in oklch, var(--border) 20%, transparent) 1px, transparent 1px),
			linear-gradient(90deg, color-mix(in oklch, var(--border) 20%, transparent) 1px, transparent 1px);
		background-size: 40px 40px;
		opacity: 0.5;
		pointer-events: none;
	}

	/* Soft vignette effect */
	.welcome-hero::after {
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
		gap: 1.75rem;
		padding: 2rem;
	}

	.welcome-tagline {
		font-family: var(--font-sans, system-ui);
		font-size: 0.9rem;
		font-weight: 400;
		color: var(--muted-foreground);
		letter-spacing: 0.15em;
		text-transform: uppercase;
		opacity: 0;
		animation: fadeInUp 0.6s ease-out 0.9s forwards;
	}

	@keyframes fadeInUp {
		from {
			opacity: 0;
			transform: translateY(20px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	@media (max-width: 640px) {
		.welcome-tagline {
			font-size: 0.75rem;
		}

		.grid-pattern {
			background-size: 30px 30px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.welcome-tagline {
			animation: none;
			opacity: 1;
		}
	}
</style>
