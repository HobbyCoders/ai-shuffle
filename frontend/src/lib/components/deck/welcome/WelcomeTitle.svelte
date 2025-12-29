<script lang="ts">
	import { onMount } from 'svelte';

	let mounted = $state(false);

	onMount(() => {
		requestAnimationFrame(() => (mounted = true));
	});
</script>

<div class="title-container" class:mounted>
	<div class="title-main">
		<span class="title-ai">AI</span>
		<span class="title-shuffle">Shuffle</span>
	</div>
	<div class="title-divider"></div>
</div>

<style>
	.title-container {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.5rem;
	}

	.title-main {
		display: flex;
		align-items: baseline;
		gap: 0.35em;
		line-height: 1;
	}

	.title-ai {
		font-family: var(--font-sans, system-ui);
		font-size: clamp(1.5rem, 4vw, 2rem);
		font-weight: 600;
		letter-spacing: 0.15em;
		color: var(--accent-cyan);
		text-shadow: 0 0 30px var(--accent-glow-strong);
		opacity: 0;
		transform: translateY(15px);
		transition:
			opacity 0.6s ease-out,
			transform 0.6s ease-out;
		transition-delay: 0.2s;
	}

	.title-container.mounted .title-ai {
		opacity: 1;
		transform: translateY(0);
	}

	.title-shuffle {
		font-family: var(--font-sans, system-ui);
		font-size: clamp(3rem, 9vw, 5rem);
		font-weight: 800;
		letter-spacing: -0.02em;
		color: var(--foreground);
		opacity: 0;
		transform: translateY(30px);
		transition:
			opacity 0.7s cubic-bezier(0.34, 1.56, 0.64, 1),
			transform 0.7s cubic-bezier(0.34, 1.56, 0.64, 1);
		transition-delay: 0.35s;
	}

	.title-container.mounted .title-shuffle {
		opacity: 1;
		transform: translateY(0);
	}

	.title-divider {
		width: 100px;
		height: 2px;
		margin-top: 0.25rem;
		background: linear-gradient(
			90deg,
			transparent 0%,
			var(--accent-cyan) 50%,
			transparent 100%
		);
		opacity: 0;
		transform: scaleX(0);
		transition:
			opacity 0.8s ease-out,
			transform 0.8s ease-out;
		transition-delay: 0.7s;
	}

	.title-container.mounted .title-divider {
		opacity: 0.7;
		transform: scaleX(1);
	}

	@media (max-width: 640px) {
		.title-shuffle {
			font-size: 2.5rem;
		}

		.title-ai {
			font-size: 1rem;
		}

		.title-divider {
			width: 80px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.title-ai,
		.title-shuffle,
		.title-divider {
			opacity: 1;
			transform: none;
			transition: none;
		}
	}
</style>
