<script lang="ts">
	import WelcomeTitle from './WelcomeTitle.svelte';
	import WelcomeCards from './WelcomeCards.svelte';
	import SpotlightEffect from './SpotlightEffect.svelte';

	interface Props {
		onCreateCard: (type: string) => void;
	}

	let { onCreateCard }: Props = $props();
</script>

<div class="welcome-hero">
	<SpotlightEffect />

	<!-- Corner ornaments -->
	<div class="ornament top-left" aria-hidden="true"></div>
	<div class="ornament top-right" aria-hidden="true"></div>
	<div class="ornament bottom-left" aria-hidden="true"></div>
	<div class="ornament bottom-right" aria-hidden="true"></div>

	<div class="welcome-content">
		<WelcomeTitle />

		<p class="welcome-tagline">What's your opening move?</p>

		<WelcomeCards {onCreateCard} />
	</div>
</div>

<style>
	.welcome-hero {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;

		/* Felt background with dramatic spotlight gradient - blueish-green matching UI */
		background:
			radial-gradient(
				ellipse 70% 55% at 50% 42%,
				oklch(0.92 0.05 85 / 0.08) 0%,
				oklch(0.92 0.05 85 / 0.03) 35%,
				oklch(0.92 0.05 85 / 0) 65%
			),
			linear-gradient(180deg,
				oklch(0.08 0.02 180) 0%,
				oklch(0.12 0.03 180) 30%,
				oklch(0.15 0.035 180) 50%,
				oklch(0.12 0.03 180) 70%,
				oklch(0.08 0.02 180) 100%
			);

		overflow: hidden;
	}

	/* Felt texture noise overlay */
	.welcome-hero::before {
		content: '';
		position: absolute;
		inset: 0;
		background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.8' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E");
		opacity: 0.035;
		pointer-events: none;
		mix-blend-mode: overlay;
	}

	/* Vignette effect */
	.welcome-hero::after {
		content: '';
		position: absolute;
		inset: 0;
		background: radial-gradient(ellipse 80% 80% at 50% 50%, transparent 30%, rgba(0, 0, 0, 0.4) 100%);
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
		letter-spacing: 0.2em;
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

	/* Corner ornaments - art deco style */
	.ornament {
		position: absolute;
		width: 50px;
		height: 50px;
		opacity: 0;
		pointer-events: none;
		z-index: 5;
		animation: fadeIn 1s ease-out 1.4s forwards;
	}

	.ornament::before,
	.ornament::after {
		content: '';
		position: absolute;
		background: var(--gold);
		opacity: 0.15;
	}

	.ornament.top-left {
		top: 2.5rem;
		left: 2.5rem;
	}
	.ornament.top-left::before {
		width: 100%;
		height: 1px;
		top: 0;
	}
	.ornament.top-left::after {
		width: 1px;
		height: 100%;
		left: 0;
	}

	.ornament.top-right {
		top: 2.5rem;
		right: 2.5rem;
	}
	.ornament.top-right::before {
		width: 100%;
		height: 1px;
		top: 0;
	}
	.ornament.top-right::after {
		width: 1px;
		height: 100%;
		right: 0;
	}

	.ornament.bottom-left {
		bottom: 2.5rem;
		left: 2.5rem;
	}
	.ornament.bottom-left::before {
		width: 100%;
		height: 1px;
		bottom: 0;
	}
	.ornament.bottom-left::after {
		width: 1px;
		height: 100%;
		left: 0;
	}

	.ornament.bottom-right {
		bottom: 2.5rem;
		right: 2.5rem;
	}
	.ornament.bottom-right::before {
		width: 100%;
		height: 1px;
		bottom: 0;
	}
	.ornament.bottom-right::after {
		width: 1px;
		height: 100%;
		right: 0;
	}

	@keyframes fadeIn {
		from {
			opacity: 0;
		}
		to {
			opacity: 1;
		}
	}

	@media (max-width: 640px) {
		.ornament {
			display: none;
		}

		.welcome-tagline {
			font-size: 0.75rem;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.welcome-tagline,
		.ornament {
			animation: none;
			opacity: 1;
		}
	}
</style>
