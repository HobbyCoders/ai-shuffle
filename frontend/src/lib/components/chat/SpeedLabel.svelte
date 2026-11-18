<script lang="ts">
	/**
	 * The effort label text with the chat's speed tier drawn into it, so the
	 * composer shows Fast or Ultrafast at a glance without opening a popover.
	 *
	 * Standard is the plain text. Fast runs a slow sheen across the letters and
	 * Ultrafast a quicker double one; both move only `background-position`, so
	 * the label never reflows. With reduced motion the text is tinted instead
	 * and a bolt (two for Ultrafast) says the same thing. The decoration is not
	 * read out: the owning control names the tier in its accessible label.
	 */
	import { Zap } from 'lucide-svelte';
	import type { ComposerSpeed } from './speed-tier';

	interface Props {
		text: string;
		speed: ComposerSpeed;
		/** Draw the reduced-motion bolt; off where a badge beside it already does. */
		bolt?: boolean;
	}

	let { text, speed, bolt = true }: Props = $props();
</script>

<span class="speed-label" data-speed={speed} class:fast={speed === 'fast'} class:ultrafast={speed === 'ultrafast'}
	><span class="speed-text" data-text={text}>{text}</span
	>{#if bolt && speed !== 'standard'}<span class="speed-bolt" aria-hidden="true"
		><Zap size={10} strokeWidth={2.25} fill="currentColor" />{#if speed === 'ultrafast'}<Zap size={10} strokeWidth={2.25} fill="currentColor" />{/if}</span
	>{/if}</span>

<style>
	.speed-label {
		display: inline-flex;
		align-items: center;
		max-width: 100%;
		min-width: 0;
	}

	.speed-text {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* The sheen: the label's own colour with a lighter band of the Fast accent
	   (the effort card's `.fast-active` colour) passing through it. */
	.fast .speed-text,
	.ultrafast .speed-text {
		--speed-band: color-mix(in oklch, var(--primary) 70%, var(--foreground));
		background-image: linear-gradient(100deg, currentColor 0%, currentColor 38%, var(--speed-band) 50%, currentColor 62%, currentColor 100%);
		background-size: 300% 100%;
		background-position: 100% 0;
		-webkit-background-clip: text;
		background-clip: text;
		-webkit-text-fill-color: transparent;
		animation: speed-sheen 3.2s ease-in-out infinite;
	}

	/* Ultrafast: two bands, a brighter accent and a quicker pass. */
	.ultrafast .speed-text {
		--speed-band: color-mix(in oklch, var(--primary) 88%, var(--foreground));
		background-image: linear-gradient(100deg,
			currentColor 0%, currentColor 30%, var(--speed-band) 38%, currentColor 46%,
			currentColor 54%, var(--speed-band) 62%, currentColor 70%, currentColor 100%);
		animation-duration: 1.3s;
		animation-timing-function: linear;
	}

	@keyframes speed-sheen {
		from { background-position: 100% 0; }
		to { background-position: 0% 0; }
	}

	/* Only drawn when motion is off: the sheen says it otherwise. */
	.speed-bolt {
		display: none;
		flex: none;
		align-items: center;
		margin-left: 3px;
		color: var(--primary);
	}

	.speed-bolt :global(svg + svg) {
		margin-left: -3px;
	}

	@media (prefers-reduced-motion: reduce) {
		.fast .speed-text,
		.ultrafast .speed-text {
			animation: none;
			background-image: none;
			-webkit-text-fill-color: currentColor;
			color: color-mix(in oklch, var(--primary) 60%, var(--foreground));
		}

		.speed-bolt {
			display: inline-flex;
		}
	}
</style>
