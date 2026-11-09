<script lang="ts">
	/**
	 * A device's link code (#674), in groups of four keycaps over a draining
	 * countdown line. Shown only on the device it belongs to and typed on the
	 * receiving worker, so a worker relaying a different key cannot match it.
	 */
	import { codeGroups, countdown } from '$lib/terminal/peer';

	let { code, left = null, total = 600, label = 'Confirmation code' }: {
		code: string;
		/** Seconds until the request expires, or null when unknown. */
		left?: number | null;
		/** The full lifetime, for the countdown line. */
		total?: number;
		label?: string;
	} = $props();

	const groups = $derived(codeGroups(code));
	// Read letter by letter: "K 7 Q 2, X M A B".
	const spoken = $derived(`${label}: ${groups.map((g) => g.split('').join(' ')).join(', ')}`);
	const ratio = $derived(left === null ? 1 : Math.max(0, Math.min(1, left / total)));
	const low = $derived(left !== null && left <= 20);
</script>

<div class="peer-code" role="group" aria-label={label}>
	<div class="caps" role="img" aria-label={spoken}>
		{#each groups as group, g}
			<span class="group" aria-hidden="true">
				{#each group.split('') as ch, i (g * 4 + i)}
					<span class="cap" style:--i={g * 3 + i}>{ch}</span>
				{/each}
			</span>
		{/each}
	</div>
	{#if left !== null}
		<div class="clock" class:low>
			<span class="line" aria-hidden="true"><span class="fill" style:transform={`scaleX(${ratio})`}></span></span>
			<span class="left">Expires in <time>{countdown(left)}</time></span>
		</div>
	{/if}
</div>

<style>
	.peer-code { display: grid; gap: 8px; justify-items: start; }
	/* A narrow screen wraps between groups, never inside one. */
	.caps { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 11px; }
	.group { display: inline-flex; gap: 3px; }
	.cap {
		display: grid; place-items: center; width: 22px; height: 32px; box-sizing: border-box;
		border:1px solid var(--border);
		border-radius: 7px; background: var(--code-bg); color: var(--foreground);
		font: 600 15px/1 var(--st-mono); letter-spacing: 0;
		box-shadow: inset 0 1px 0 color-mix(in oklch, var(--foreground) 6%, transparent);
		animation: cap-in 0.32s cubic-bezier(0.2, 0.8, 0.2, 1) both; animation-delay: calc(var(--i) * 35ms);
	}
	.clock { display: grid; gap: 4px; width: 100%; max-width: 212px; font: 500 11.5px var(--font-sans); color: var(--muted-foreground); }
	.line { display: block; height: 2px; border-radius: 2px; background: var(--muted); overflow: hidden; }
	.fill { display: block; height: 100%; background:var(--muted-foreground); transform-origin: left center; transition: transform 1s linear; }
	.clock.low { color: var(--warning); }
	.clock.low .fill { background: var(--warning); }
	time { font-variant-numeric: tabular-nums; color: var(--foreground); }
	.clock.low time { color: var(--warning); }
	@keyframes cap-in { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: none; } }
	@media (max-width: 700px) { .cap { width: 20px; height: 38px; font-size: 16px; } }
	@media (prefers-reduced-motion: reduce) { .cap { animation: none; } .fill { transition: none; } }
</style>
