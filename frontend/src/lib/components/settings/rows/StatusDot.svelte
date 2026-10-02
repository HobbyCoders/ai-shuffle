<script lang="ts">
	/**
	 * StatusDot: ok (green), warn (amber), err (red), off (hollow),
	 * standby (hollow, stronger ring), busy (pulsing). `label` adds a
	 * screen-reader word; without it the dot is decorative and the text
	 * beside it carries the meaning.
	 */
	import type { StatusTone } from '$lib/settings/types';

	interface Props {
		tone: StatusTone;
		label?: string;
		/** Ring color for dots that sit on a tile (matches the tile's background). */
		ring?: 'bg' | 'accent' | 'none';
		size?: number;
	}

	let { tone, label, ring = 'none', size = 8 }: Props = $props();
</script>

<span
	class="st-dot {tone}"
	class:ring-bg={ring === 'bg'}
	class:ring-accent={ring === 'accent'}
	style:width="{size}px"
	style:height="{size}px"
	role={label ? 'img' : undefined}
	aria-label={label}
	aria-hidden={label ? undefined : 'true'}
></span>

<style>
	.st-dot {
		display: inline-block;
		border-radius: 50%;
		background: var(--st-subtle);
		flex: none;
		--ring: transparent;
	}
	.st-dot.ok {
		background: var(--st-success);
	}
	.st-dot.warn {
		background: var(--st-warning);
	}
	.st-dot.err {
		background: var(--st-destructive);
	}
	.st-dot.busy {
		background: var(--st-mfg);
		animation: st-pulse 1.4s ease-in-out infinite;
	}
	.st-dot.off {
		background: transparent;
		box-shadow: inset 0 0 0 1.5px var(--st-subtle);
	}
	.st-dot.standby {
		background: transparent;
		box-shadow: inset 0 0 0 1.5px var(--st-mfg);
	}
	.st-dot.ring-bg {
		--ring: var(--st-bg);
	}
	.st-dot.ring-accent {
		--ring: var(--st-accent);
	}
	.st-dot.ring-bg,
	.st-dot.ring-accent {
		box-shadow: 0 0 0 2px var(--ring);
	}
	.st-dot.off.ring-bg,
	.st-dot.off.ring-accent {
		background: var(--ring);
		box-shadow:
			inset 0 0 0 1.5px var(--st-subtle),
			0 0 0 2px var(--ring);
	}
	.st-dot.standby.ring-bg,
	.st-dot.standby.ring-accent {
		background: var(--ring);
		box-shadow:
			inset 0 0 0 1.5px var(--st-mfg),
			0 0 0 2px var(--ring);
	}
	@keyframes st-pulse {
		50% {
			opacity: 0.3;
		}
	}
</style>
