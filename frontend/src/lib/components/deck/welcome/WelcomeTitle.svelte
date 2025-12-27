<script lang="ts">
	import { onMount } from 'svelte';

	let mounted = $state(false);

	onMount(() => {
		requestAnimationFrame(() => (mounted = true));
	});

	const letters = ['S', 'H', 'U', 'F', 'F', 'L', 'E'];
</script>

<div class="title-container" class:mounted>
	<span class="title-prefix">ai</span>
	<div class="title-deck">
		{#each letters as letter, i}
			<span class="title-letter" style="--delay: {i * 60}ms" class:gold={i === 0}>
				{letter}
			</span>
		{/each}
	</div>
	<div class="title-divider"></div>
</div>

<style>
	.title-container {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.25rem;
	}

	.title-prefix {
		font-family: var(--font-sans, system-ui);
		font-size: 0.85rem;
		font-weight: 500;
		letter-spacing: 0.5em;
		color: var(--muted-foreground);
		text-transform: lowercase;
		opacity: 0;
		transform: translateY(15px);
		transition:
			opacity 0.6s ease-out,
			transform 0.6s ease-out;
		transition-delay: 0.2s;
	}

	.title-container.mounted .title-prefix {
		opacity: 1;
		transform: translateY(0);
	}

	.title-deck {
		display: flex;
		gap: 0.02em;
		line-height: 1;
	}

	.title-letter {
		font-family: var(--font-display, Georgia, serif);
		font-size: clamp(3.5rem, 10vw, 6rem);
		font-weight: 900;
		color: var(--foreground);
		opacity: 0;
		transform: translateY(30px);
		transition:
			opacity 0.7s cubic-bezier(0.34, 1.56, 0.64, 1),
			transform 0.7s cubic-bezier(0.34, 1.56, 0.64, 1);
		transition-delay: calc(0.3s + var(--delay, 0ms));
	}

	.title-container.mounted .title-letter {
		opacity: 1;
		transform: translateY(0);
	}

	.title-letter.gold {
		color: var(--gold);
		text-shadow:
			0 0 60px var(--gold-glow-strong),
			0 0 120px var(--gold-glow);
	}

	.title-divider {
		width: 140px;
		height: 1px;
		margin-top: 0.5rem;
		background: linear-gradient(90deg, transparent 0%, var(--gold) 50%, transparent 100%);
		opacity: 0;
		transform: scaleX(0);
		transition:
			opacity 0.8s ease-out,
			transform 0.8s ease-out;
		transition-delay: 0.7s;
	}

	.title-container.mounted .title-divider {
		opacity: 0.6;
		transform: scaleX(1);
	}

	@media (max-width: 640px) {
		.title-letter {
			font-size: 2.5rem;
		}

		.title-prefix {
			font-size: 0.7rem;
		}

		.title-divider {
			width: 100px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.title-prefix,
		.title-letter,
		.title-divider {
			opacity: 1;
			transform: none;
			transition: none;
		}

		.title-letter.gold {
			color: var(--gold);
		}
	}
</style>
