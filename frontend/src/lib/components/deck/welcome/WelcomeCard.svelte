<script lang="ts">
	import type { Component } from 'svelte';

	interface Props {
		type: string;
		label: string;
		description: string;
		icon: Component;
		suit: '♠' | '♥' | '♦' | '♣';
		shortcut: string;
		rotation: number;
		index: number;
		onclick: () => void;
	}

	let { type, label, description, icon: Icon, suit, shortcut, rotation, index, onclick }: Props =
		$props();

	let cardEl: HTMLButtonElement | undefined = $state();
	let isHovered = $state(false);

	function handleMouseMove(e: MouseEvent) {
		if (!cardEl) return;
		const rect = cardEl.getBoundingClientRect();
		const x = (e.clientX - rect.left) / rect.width - 0.5;
		const y = (e.clientY - rect.top) / rect.height - 0.5;

		cardEl.style.setProperty('--mouse-x', `${x * 15}deg`);
		cardEl.style.setProperty('--mouse-y', `${y * -15}deg`);
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
	style="--rotation: {rotation}deg; --index: {index}"
	onmouseenter={() => (isHovered = true)}
	onmouseleave={handleMouseLeave}
	onmousemove={handleMouseMove}
	{onclick}
>
	<span class="card-suit top-left">{suit}</span>
	<span class="card-suit bottom-right">{suit}</span>

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
		--mouse-x: 0deg;
		--mouse-y: 0deg;
		--index: 0;

		position: relative;
		width: 150px;
		height: 210px;
		padding: 1.25rem 1rem;

		background: linear-gradient(155deg, var(--card-surface-welcome) 0%, var(--card-bg-welcome) 100%);
		border: 1px solid var(--border);
		border-radius: 14px;

		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 0.6rem;

		cursor: pointer;
		transform-style: preserve-3d;
		transform:
			rotateZ(var(--rotation))
			rotateY(var(--mouse-x))
			rotateX(var(--mouse-y));

		opacity: 0;
		animation: dealCard 0.6s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
		animation-delay: calc(1s + var(--index) * 0.1s);

		/* Spring bounce transition - cubic-bezier overshoots for bounce */
		/* Override global * selector transition from app.css */
		transition-property: transform, border-color, box-shadow !important;
		transition-duration: 0.4s, 0.2s, 0.3s !important;
		transition-timing-function: cubic-bezier(0.34, 1.56, 0.64, 1), ease, ease !important;

		box-shadow:
			0 8px 24px rgba(0, 0, 0, 0.4),
			0 4px 8px rgba(0, 0, 0, 0.3);
	}

	.welcome-card:hover {
		transform:
			rotateZ(0deg)
			rotateY(var(--mouse-x))
			rotateX(var(--mouse-y))
			translateY(-16px)
			scale(1.06);
		border-color: var(--gold);

		box-shadow:
			0 28px 56px rgba(0, 0, 0, 0.5),
			0 14px 28px rgba(0, 0, 0, 0.4),
			0 0 50px var(--gold-glow),
			inset 0 1px 0 rgba(255, 255, 255, 0.08);

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

	@keyframes dealCard {
		from {
			opacity: 0;
			transform: translateX(120px) rotateZ(calc(var(--rotation) + 20deg)) scale(0.7);
		}
		to {
			opacity: 1;
			transform: rotateZ(var(--rotation)) scale(1);
		}
	}

	/* Card Suit */
	.card-suit {
		position: absolute;
		font-size: 1rem;
		color: var(--gold);
		opacity: 0.4;
		transition: opacity 0.2s ease;
	}

	.card-suit.top-left {
		top: 0.6rem;
		left: 0.6rem;
	}

	.card-suit.bottom-right {
		bottom: 0.6rem;
		right: 0.6rem;
		transform: rotate(180deg);
	}

	.welcome-card:hover .card-suit {
		opacity: 0.8;
	}

	/* Card Content */
	.card-content {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.5rem;
	}

	.card-icon {
		width: 60px;
		height: 60px;
		display: flex;
		align-items: center;
		justify-content: center;
		border-radius: 50%;
		background: rgba(255, 255, 255, 0.04);
		color: var(--foreground);
		transition: all 0.25s ease-out;
	}

	.welcome-card:hover .card-icon {
		background: var(--gold-glow);
		color: var(--gold);
		box-shadow: 0 0 24px var(--gold-glow);
	}

	.card-label {
		font-family: var(--font-sans, system-ui);
		font-size: 0.85rem;
		font-weight: 600;
		letter-spacing: 0.12em;
		color: var(--foreground);
		text-transform: uppercase;
	}

	.card-description {
		font-size: 0.7rem;
		color: var(--muted-foreground);
		text-align: center;
		line-height: 1.4;
		max-width: 100px;
	}

	/* Keyboard Shortcut */
	.card-shortcut {
		position: absolute;
		bottom: 0.75rem;
		font-family: ui-monospace, 'Monaco', 'Menlo', monospace;
		font-size: 0.6rem;
		padding: 0.2rem 0.5rem;
		background: rgba(255, 255, 255, 0.05);
		border-radius: 4px;
		color: var(--muted-foreground);
		opacity: 0;
		transform: translateY(4px);
		transition: all 0.2s ease-out;
	}

	.welcome-card:hover .card-shortcut {
		opacity: 1;
		transform: translateY(0);
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
