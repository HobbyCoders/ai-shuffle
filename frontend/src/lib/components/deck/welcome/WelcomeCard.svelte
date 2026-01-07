<script lang="ts">
	import type { Component, ComponentType } from 'svelte';
	import { onMount } from 'svelte';

	interface Props {
		type: string;
		label: string;
		description: string;
		icon: ComponentType | Component<any>;
		shortcut: string;
		rotation: number;
		offsetY?: number;
		index: number;
		onclick: () => void;
	}

	let { type, label, description, icon: Icon, shortcut, rotation, offsetY = 0, index, onclick }: Props =
		$props();

	let cardEl: HTMLButtonElement | undefined = $state();
	let isHovered = $state(false);
	let animationComplete = $state(false);

	onMount(() => {
		// Wait for deal animation to complete, then enable hover transitions
		const delay = 1000 + index * 100 + 600;
		const timer = setTimeout(() => {
			animationComplete = true;
		}, delay);
		return () => clearTimeout(timer);
	});

	function handleMouseMove(e: MouseEvent) {
		if (!cardEl) return;
		const rect = cardEl.getBoundingClientRect();
		const x = (e.clientX - rect.left) / rect.width - 0.5;
		const y = (e.clientY - rect.top) / rect.height - 0.5;

		cardEl.style.setProperty('--mouse-x', `${x * 12}deg`);
		cardEl.style.setProperty('--mouse-y', `${y * -12}deg`);
	}

	function handleMouseLeave() {
		isHovered = false;
		cardEl?.style.setProperty('--mouse-x', '0deg');
		cardEl?.style.setProperty('--mouse-y', '0deg');
	}
</script>

<button
	bind:this={cardEl}
	class="welcome-card"
	class:hovered={isHovered}
	class:animation-complete={animationComplete}
	style="--rotation: {rotation}deg; --offset-y: {offsetY}px; --index: {index}"
	onmouseenter={() => (isHovered = true)}
	onmouseleave={handleMouseLeave}
	onmousemove={handleMouseMove}
	{onclick}
>
	<div class="card-content">
		<div class="card-icon">
			<Icon size={28} strokeWidth={1.5} />
		</div>
		<span class="card-label">{label}</span>
		<span class="card-description">{description}</span>
	</div>

	<kbd class="card-shortcut">{shortcut}</kbd>
</button>

<style>
	.welcome-card {
		--rotation: 0deg;
		--offset-y: 0px;
		--mouse-x: 0deg;
		--mouse-y: 0deg;
		--index: 0;

		position: relative;
		width: 150px;
		height: 200px;
		padding: 1.25rem 1rem;

		/* Use theme card color instead of hardcoded dark value */
		background: var(--card-surface-welcome, var(--card));
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);

		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 0.6rem;

		cursor: pointer;
		transform-style: preserve-3d;
		transform:
			translateY(var(--offset-y))
			rotateZ(var(--rotation))
			rotateY(var(--mouse-x))
			rotateX(var(--mouse-y));

		opacity: 0;
		animation: dealCard 0.6s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
		animation-delay: calc(1s + var(--index) * 0.1s);

		transition-property: transform, border-color, box-shadow !important;
		transition-duration: 0.4s, 0.2s, 0.3s !important;
		transition-timing-function: cubic-bezier(0.34, 1.56, 0.64, 1), ease, ease !important;

		box-shadow: var(--shadow-m);
	}

	.welcome-card:hover {
		transform:
			rotateZ(0deg)
			rotateY(var(--mouse-x))
			rotateX(var(--mouse-y))
			translateY(-14px)
			scale(1.04);
		/* Use theme primary color for hover border */
		border-color: var(--primary);

		box-shadow:
			var(--shadow-l),
			0 0 40px var(--glow-color-soft);

		z-index: 10;
	}

	.welcome-card:active {
		transform:
			rotateZ(0deg)
			translateY(-6px)
			scale(0.97);
		transition-duration: 0.1s !important;
		transition-timing-function: ease-out !important;
	}

	.welcome-card.animation-complete {
		animation: none;
		opacity: 1;
		transform: translateY(var(--offset-y)) rotateZ(var(--rotation)) rotateY(var(--mouse-x)) rotateX(var(--mouse-y));
	}

	.welcome-card.animation-complete:hover {
		transform:
			rotateZ(0deg)
			rotateY(var(--mouse-x))
			rotateX(var(--mouse-y))
			translateY(-14px)
			scale(1.04);
	}

	@keyframes dealCard {
		from {
			opacity: 0;
			transform: translateY(var(--offset-y)) translateX(100px) rotateZ(calc(var(--rotation) + 15deg)) scale(0.8);
		}
		to {
			opacity: 1;
			transform: translateY(var(--offset-y)) rotateZ(var(--rotation)) scale(1);
		}
	}

	/* Card Content */
	.card-content {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.6rem;
	}

	.card-icon {
		width: 56px;
		height: 56px;
		display: flex;
		align-items: center;
		justify-content: center;
		border-radius: calc(var(--radius, 0.75rem) * 1.17);
		/* Use theme-aware subtle background */
		background: color-mix(in oklch, var(--muted) 30%, transparent);
		border: 1px solid var(--border);
		color: var(--muted-foreground);
		transition: all 0.25s ease-out;
	}

	.welcome-card:hover .card-icon {
		/* Use theme primary color for hover state */
		background: var(--glow-color-soft);
		border-color: var(--primary);
		color: var(--primary);
		box-shadow: 0 0 20px var(--glow-color-soft);
	}

	.card-label {
		font-family: var(--font-sans, system-ui);
		font-size: 0.8rem;
		font-weight: 600;
		letter-spacing: 0.1em;
		color: var(--foreground);
		text-transform: uppercase;
	}

	.card-description {
		font-size: 0.7rem;
		color: var(--muted-foreground);
		text-align: center;
		line-height: 1.4;
		max-width: 110px;
	}

	/* Keyboard Shortcut */
	.card-shortcut {
		position: absolute;
		bottom: 0.75rem;
		font-family: ui-monospace, 'Monaco', 'Menlo', monospace;
		font-size: 0.6rem;
		padding: 0.25rem 0.5rem;
		/* Use theme-aware colors */
		background: color-mix(in oklch, var(--muted) 40%, transparent);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--muted-foreground);
		opacity: 0;
		transform: translateY(4px);
		transition: all 0.2s ease-out;
	}

	.welcome-card:hover .card-shortcut {
		opacity: 1;
		transform: translateY(0);
		border-color: color-mix(in oklch, var(--primary) 30%, var(--border));
	}

	/* Mobile adjustments */
	@media (max-width: 640px) {
		.welcome-card {
			width: 180px;
			height: 140px;
			--rotation: 0deg !important;
			animation-name: fadeInUp;
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
	}

	@media (prefers-reduced-motion: reduce) {
		.welcome-card {
			animation: none;
			opacity: 1;
			transform: rotateZ(var(--rotation, 0deg));
			transition: border-color 0.2s ease, box-shadow 0.2s ease;
		}

		.welcome-card:hover {
			transform: rotateZ(0deg) translateY(-4px);
		}
	}
</style>
