<script lang="ts">
	import { createEventDispatcher, onMount, onDestroy } from 'svelte';

	// Props
	export let container: HTMLElement | null = null;
	export let threshold: number = 80;
	export let maxPull: number = 150;
	export let disabled: boolean = false;

	const dispatch = createEventDispatcher<{
		refresh: void;
	}>();

	let isPulling = false;
	let pullDistance = 0;
	let isRefreshing = false;
	let startY = 0;
	let currentY = 0;

	// Computed
	$: pullProgress = Math.min(pullDistance / threshold, 1);
	$: isTriggered = pullDistance >= threshold;
	$: rotation = pullProgress * 180;

	function getScrollTop(): number {
		if (container) {
			return container.scrollTop;
		}
		return window.scrollY || document.documentElement.scrollTop;
	}

	function handleTouchStart(event: TouchEvent) {
		if (disabled || isRefreshing) return;
		if (getScrollTop() > 0) return;

		startY = event.touches[0].clientY;
		isPulling = true;
	}

	function handleTouchMove(event: TouchEvent) {
		if (!isPulling || disabled || isRefreshing) return;

		currentY = event.touches[0].clientY;
		const deltaY = currentY - startY;

		if (deltaY > 0 && getScrollTop() === 0) {
			event.preventDefault();
			// Apply resistance to make it feel natural
			pullDistance = Math.min(deltaY * 0.5, maxPull);
		}
	}

	function handleTouchEnd() {
		if (!isPulling) return;

		isPulling = false;

		if (pullDistance >= threshold && !isRefreshing) {
			isRefreshing = true;
			dispatch('refresh');

			// Auto-reset after a delay if parent doesn't call reset
			setTimeout(() => {
				reset();
			}, 3000);
		} else {
			pullDistance = 0;
		}

		startY = 0;
		currentY = 0;
	}

	export function reset() {
		isRefreshing = false;
		pullDistance = 0;
	}

	onMount(() => {
		const target = container || document.body;

		target.addEventListener('touchstart', handleTouchStart as EventListener, { passive: true });
		target.addEventListener('touchmove', handleTouchMove as EventListener, { passive: false });
		target.addEventListener('touchend', handleTouchEnd as EventListener, { passive: true });

		return () => {
			target.removeEventListener('touchstart', handleTouchStart as EventListener);
			target.removeEventListener('touchmove', handleTouchMove as EventListener);
			target.removeEventListener('touchend', handleTouchEnd as EventListener);
		};
	});
</script>

{#if pullDistance > 0 || isRefreshing}
	<div
		class="pull-to-refresh-indicator fixed top-0 left-0 right-0 z-50 flex items-center justify-center transition-transform duration-200"
		style="transform: translateY({Math.min(pullDistance, maxPull) - 60}px)"
	>
		<div
			class="w-10 h-10 rounded-full bg-[var(--color-bg-secondary)] border border-[var(--color-border)]
				   shadow-lg flex items-center justify-center"
		>
			{#if isRefreshing}
				<svg class="w-5 h-5 text-[var(--color-primary)] animate-spin" fill="none" viewBox="0 0 24 24">
					<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
					<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
				</svg>
			{:else}
				<svg
					class="w-5 h-5 transition-transform duration-200"
					class:text-[var(--color-primary)]={isTriggered}
					class:text-[var(--color-text-secondary)]={!isTriggered}
					style="transform: rotate({rotation}deg)"
					fill="none"
					stroke="currentColor"
					viewBox="0 0 24 24"
				>
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 14l-7 7m0 0l-7-7m7 7V3" />
				</svg>
			{/if}
		</div>
	</div>
{/if}

<style>
	.pull-to-refresh-indicator {
		pointer-events: none;
	}
</style>
