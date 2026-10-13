<script lang="ts">
	/**
	 * Reasoning effort as rising bars: one bar per level, lit up to the chosen
	 * one. The quick bar and the effort picker draw the level with it instead
	 * of a word, so the state reads at a glance. Decorative: the control that
	 * holds it carries the label.
	 */
	let { index, count, size = 20 }: { index: number; count: number; size?: number } = $props();

	const bars = $derived.by(() => {
		const n = Math.max(1, count);
		const step = 18 / n;
		const width = Math.max(2.4, step * 0.55);
		return Array.from({ length: n }, (_, i) => {
			const h = 5 + (12 * i) / Math.max(1, n - 1);
			return { x: 3 + i * step + (step - width) / 2, y: 20 - h, w: width, h, on: i <= index };
		});
	});
</script>

<svg class="effort-meter" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
	{#each bars as bar, i (i)}
		<rect x={bar.x.toFixed(1)} y={bar.y.toFixed(1)} width={bar.w.toFixed(1)} height={bar.h.toFixed(1)} rx="1" fill="currentColor" opacity={bar.on ? 1 : 0.28} />
	{/each}
</svg>

<style>
	.effort-meter { display: block; flex-shrink: 0; }
</style>
