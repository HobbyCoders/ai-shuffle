<script lang="ts">
	/**
	 * Away for all chats: the owner's one switch for going to bed, shared by
	 * every worker. The same control is the switch while off and the indicator
	 * while on, so whatever shows that it is on also turns it off.
	 *
	 * Homes: the desktop rail's foot (`rail`, on every screen from 768px), the
	 * phone top bars (`bar`, in the mobile shell and the responsive web bar under
	 * 768px) and the mobile drawer's header (`drawer`, which covers the bar).
	 *
	 * The slot keeps its size in every state, including before the first answer
	 * and for the whole time a tap is in flight, so nothing beside it moves. The
	 * switch only shows what the worker confirmed: until then it stays out.
	 */
	import { LoaderCircle, Moon } from 'lucide-svelte';
	import { awayMode, globalAwayOnMessage } from '$lib/stores/away-mode.svelte';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import { notify } from '$lib/stores/notifications';

	interface Props {
		variant: 'rail' | 'bar' | 'drawer';
	}

	let { variant }: Props = $props();

	const on = $derived(awayMode.globalAway);
	const busy = $derived(awayMode.globalBusy);
	const hint = $derived(on
		? 'Away for all chats is on: agents take recommended defaults. Click to turn it off.'
		: 'Away for all chats: agents stop waiting for you and take recommended defaults.');

	async function toggle() {
		if (awayMode.globalBusy) return;
		const next = !awayMode.globalAway;
		try {
			const result = await awayMode.setGlobal(next);
			if (next && result.away) notify(globalAwayOnMessage(result), { severity: 'info' });
		} catch {
			notify(`Could not turn it ${next ? 'on' : 'off'}. Check the connection and try again.`, {
				severity: 'error',
				title: 'Away for all chats'
			});
		}
	}
</script>

{#if $isAdmin}
	<div class="slot {variant}">
		{#if awayMode.globalLoaded}
			<button
				type="button"
				class="away"
				class:on
				role="switch"
				aria-checked={on}
				aria-label="Away for all chats"
				aria-busy={busy || undefined}
				aria-disabled={busy || undefined}
				title={variant === 'rail' ? undefined : hint}
				data-away-all-chats
				onclick={toggle}
			>
				<span class="face">
					{#if busy}
						<LoaderCircle class="away-spin" size={16} strokeWidth={2} aria-hidden="true" />
					{:else}
						<Moon size={on ? 15 : 18} strokeWidth={1.75} fill={on ? 'currentColor' : 'none'} aria-hidden="true" />
					{/if}
					{#if on}<span class="word">Away</span>{/if}
				</span>
				{#if variant === 'rail'}<span class="tip" aria-hidden="true">{hint}</span>{/if}
			</button>
		{/if}
	</div>
{/if}

<style>
	.slot {
		position: relative;
		flex: none;
		display: grid;
		place-items: center;
	}
	.slot.rail { width: 56px; height: 44px; }
	.slot.bar { width: 44px; height: 48px; }
	.slot.drawer { width: 44px; height: 44px; }

	.away {
		--away-ink: color-mix(in oklab, var(--warning) 62%, var(--foreground));
		position: relative;
		width: 100%;
		height: 100%;
		display: grid;
		place-items: center;
		padding: 0;
		border: 0;
		border-radius: var(--radius-control);
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		-webkit-tap-highlight-color: transparent;
		user-select: none;
		transition: color 0.13s ease;
	}
	.away:hover { color: var(--foreground); }
	.away:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.away[aria-busy='true'] { cursor: progress; }

	/* Drawn inside the target, so a 44px tap area can hold a smaller mark. */
	.face {
		display: grid;
		place-items: center;
		align-content: center;
		width: 36px;
		height: 36px;
		border-radius: var(--radius-control);
		transition: background-color 0.13s ease, box-shadow 0.13s ease;
	}
	.away:hover .face { background: var(--row-hover); }
	.away:active .face { background: var(--rail-selected); }

	/* On: moon and word together, tinted from the warning token, in the same
	   footprint as off. Colour is never the only sign. */
	.away.on { color: var(--away-ink); }
	.away.on .face {
		width: 42px;
		height: 40px;
		gap: 1px;
		background: color-mix(in oklab, var(--warning) 14%, transparent);
		box-shadow: inset 0 0 0 1px color-mix(in oklab, var(--warning) 42%, transparent);
	}
	.away.on:hover { color: var(--away-ink); }
	.away.on:hover .face { background: color-mix(in oklab, var(--warning) 24%, transparent); }
	.away.on:active .face { background: color-mix(in oklab, var(--warning) 32%, transparent); }

	.word {
		font-size: 10.5px;
		line-height: 12px;
		font-weight: 600;
		letter-spacing: 0.01em;
	}

	:global(.away-spin) { animation: away-spin 0.9s linear infinite; }
	@keyframes away-spin { to { transform: rotate(360deg); } }

	/* The rail's tooltip, its look copied from SidebarRail's. */
	.tip {
		position: absolute;
		left: calc(100% + 2px);
		bottom: 2px;
		z-index: 9;
		width: max-content;
		max-width: 260px;
		padding: 4px 8px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--popover);
		color: var(--foreground);
		font-size: var(--fs-label);
		font-weight: 400;
		line-height: 1.35;
		text-align: left;
		box-shadow: var(--shadow-m);
		opacity: 0;
		pointer-events: none;
	}
	.away:hover .tip,
	.away:focus-visible .tip { opacity: 1; transition: opacity 0.1s ease 0.45s; }
	.away:active .tip { opacity: 0; transition: none; }
	@media (hover: none) { .tip { display: none; } }

	@media (prefers-reduced-motion: reduce) {
		.away, .face, .tip { transition: none; }
		:global(.away-spin) { animation-duration: 2.4s; }
	}
</style>
