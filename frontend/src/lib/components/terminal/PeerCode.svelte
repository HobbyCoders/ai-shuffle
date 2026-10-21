<script lang="ts">
	/**
	 * The six-character confirmation code of a peer-terminal request (#196),
	 * drawn the same way on both workers so the owner can compare them at a
	 * glance: two groups of three keycaps over a draining countdown line.
	 * The code is a comparison aid, not a secret and not authority.
	 */
	import { codeGroups, countdown } from '$lib/terminal/peer';

	let { code, left = null, total = 120, label = 'Confirmation code' }: {
		code: string;
		/** Seconds until the request expires, or null when unknown. */
		left?: number | null;
		/** The full lifetime, for the countdown line. */
		total?: number;
		label?: string;
	} = $props();

	const groups = $derived(codeGroups(code));
	// Read letter by letter: "K 7 Q, 2 X M".
	const spoken = $derived(`${label}: ${groups.map((g) => g.split('').join(' ')).join(', ')}`);
	const ratio = $derived(left === null ? 1 : Math.max(0, Math.min(1, left / total)));
	const low = $derived(left !== null && left <= 20);
</script>

<div class="peer-code" role="group" aria-label={label}>
	<div class="caps" role="img" aria-label={spoken}>
		{#each groups as group, g}
			{#if g}<span class="gap" aria-hidden="true"></span>{/if}
			{#each group.split('') as ch, i (g * 3 + i)}
				<span class="cap" aria-hidden="true" style:--i={g * 3 + i}>{ch}</span>
			{/each}
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
	.caps { display: flex; align-items: center; gap: 4px; }
	.gap { width: 8px; }
	.cap {
		display: grid; place-items: center; width: 30px; height: 38px; box-sizing: border-box;
		border: 1px solid color-mix(in oklch, var(--primary) 34%, var(--border-strong)); border-bottom-width: 3px;
		border-radius: 7px; background: var(--code-bg); color: var(--foreground);
		font: 700 19px/1 var(--font-mono); letter-spacing: 0;
		box-shadow: inset 0 1px 0 color-mix(in oklch, var(--foreground) 6%, transparent);
		animation: cap-in 0.32s cubic-bezier(0.2, 0.8, 0.2, 1) both; animation-delay: calc(var(--i) * 35ms);
	}
	.clock { display: grid; gap: 4px; width: 100%; max-width: 212px; font: 500 11.5px var(--font-sans); color: var(--muted-foreground); }
	.line { display: block; height: 2px; border-radius: 2px; background: var(--muted); overflow: hidden; }
	.fill { display: block; height: 100%; background: var(--primary); transform-origin: left center; transition: transform 1s linear; }
	.clock.low { color: var(--warning); }
	.clock.low .fill { background: var(--warning); }
	time { font-family: var(--font-mono); font-variant-numeric: tabular-nums; color: var(--foreground); }
	.clock.low time { color: var(--warning); }
	@keyframes cap-in { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: none; } }
	@media (max-width: 700px) { .cap { width: 34px; height: 42px; font-size: 21px; } }
	@media (prefers-reduced-motion: reduce) { .cap { animation: none; } .fill { transition: none; } }
</style>
