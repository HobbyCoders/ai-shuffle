<script lang="ts">
	/**
	 * HelpTip: the "?" button. Shows its text on hover, focus or click
	 * (role=tooltip, wired with aria-describedby). Esc closes it. Use for
	 * detail that does not fit the 5-word hint; keep it rare.
	 */
	import { CircleHelp } from 'lucide-svelte';
	import { tick } from 'svelte';

	interface Props {
		text: string;
		/** What the tip is about, for the button's accessible name. */
		about?: string;
	}

	let { text, about }: Props = $props();

	const uid = $props.id();
	const tipId = `${uid}-tip`;
	let open = $state(false);
	let pinned = $state(false);
	let button: HTMLButtonElement | undefined = $state();
	let tip: HTMLDivElement | undefined = $state();
	let left = $state(0);
	let top = $state(0);

	async function show() {
		open = true;
		await tick();
		place();
	}

	function hide() {
		open = false;
		pinned = false;
	}

	function place() {
		if (!button || typeof window === 'undefined') return;
		const r = button.getBoundingClientRect();
		const w = tip?.offsetWidth ?? 300;
		const h = tip?.offsetHeight ?? 0;
		left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left - 10));
		const below = r.bottom + 8;
		top = below + h > window.innerHeight - 8 ? Math.max(8, r.top - h - 8) : below;
	}

	function onclick(event: MouseEvent) {
		event.preventDefault();
		if (open && pinned) hide();
		else {
			pinned = true;
			void show();
		}
	}

	function onkeydown(event: KeyboardEvent) {
		if (event.key === 'Escape' && open) {
			event.stopPropagation();
			hide();
		}
	}
</script>

<button
	bind:this={button}
	type="button"
	class="st-help"
	aria-label={about ? `About ${about}` : 'More info'}
	aria-describedby={open ? tipId : undefined}
	onmouseenter={show}
	onmouseleave={() => {
		if (!pinned) open = false;
	}}
	onfocus={show}
	onblur={hide}
	{onclick}
	{onkeydown}
>
	<CircleHelp />
</button>
{#if open}
	<div bind:this={tip} id={tipId} role="tooltip" class="st-tip" style:left="{left}px" style:top="{top}px">{text}</div>
{/if}

<style>
	.st-help {
		display: inline-grid;
		place-items: center;
		width: 18px;
		height: 18px;
		border-radius: 50%;
		border: 0;
		background: none;
		color: var(--st-subtle);
		padding: 0;
		flex: none;
	}
	.st-help :global(svg.lucide) {
		width: 14px;
		height: 14px;
	}
	.st-help:hover,
	.st-help:focus-visible {
		color: var(--st-fg);
	}
	.st-tip {
		position: fixed;
		z-index: 95;
		max-width: 300px;
		background: var(--st-popover);
		border: 1px solid var(--st-border-strong);
		border-radius: var(--st-radius-md);
		padding: 10px 12px;
		font-size: 12.5px;
		font-weight: 400;
		line-height: 1.5;
		color: var(--st-fg);
		box-shadow: var(--st-shadow);
		pointer-events: none;
		white-space: normal;
	}
</style>
