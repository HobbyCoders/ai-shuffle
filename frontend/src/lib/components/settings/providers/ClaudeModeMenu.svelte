<script lang="ts">
	/**
	 * What Claude runs on (Subscription, API key, Cloud, Off), on Claude's first
	 * account row. Separate from choosing among saved keys and sign-ins: "Use" on
	 * a saved account never changes this. A button and a small menu rather than a
	 * native select, so it sits at the row's type size without the phone's
	 * 16px-or-it-zooms rule applying.
	 */
	import { tick } from 'svelte';
	import { Check, ChevronDown } from 'lucide-svelte';
	import type { ClaudeAuthMode } from '$lib/api/claude-auth';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import { MODE_OPTIONS, type ProviderView } from './providers-model';
	import { pickClaudeMode } from './provider-actions';

	let { provider }: { provider: ProviderView } = $props();

	const access = getSettingsAccess();
	const uid = $props.id();
	const mode = $derived(providers.current.claude?.mode ?? 'subscription');
	const shown = $derived(MODE_OPTIONS.find((o) => o.value === mode)?.label ?? mode);
	const off = $derived(!providers.current.claude || access.readOnly);

	let open = $state(false);
	let button: HTMLButtonElement | undefined = $state();
	let menu: HTMLDivElement | undefined = $state();
	let left = $state(0);
	let top = $state(0);

	const items = () => Array.from(menu?.querySelectorAll<HTMLElement>('[role="menuitemradio"]') ?? []);

	async function show() {
		if (off) return;
		open = true;
		await tick();
		if (!button || !menu) return;
		const r = button.getBoundingClientRect();
		left = Math.max(8, Math.min(window.innerWidth - menu.offsetWidth - 8, r.right - menu.offsetWidth));
		const below = r.bottom + 6;
		top = below + menu.offsetHeight > window.innerHeight - 8 ? Math.max(8, r.top - menu.offsetHeight - 6) : below;
		(items().find((el) => el.getAttribute('aria-checked') === 'true') ?? items()[0])?.focus();
	}

	function hide(refocus = true) {
		open = false;
		if (refocus) button?.focus();
	}

	function pick(value: ClaudeAuthMode) {
		hide();
		if (value !== mode) void pickClaudeMode(provider, value);
	}

	function onkeydown(event: KeyboardEvent) {
		const list = items();
		const i = list.indexOf(document.activeElement as HTMLElement);
		const move = (to: number) => {
			event.preventDefault();
			list[(to + list.length) % list.length]?.focus();
		};
		if (event.key === 'Escape') {
			event.preventDefault();
			event.stopPropagation();
			hide();
		} else if (event.key === 'ArrowDown') move(i + 1);
		else if (event.key === 'ArrowUp') move(i - 1);
		else if (event.key === 'Home') move(0);
		else if (event.key === 'End') move(list.length - 1);
		else if (event.key === 'Tab') hide(false);
	}

	function onWindowPointer(event: PointerEvent) {
		if (!open) return;
		const target = event.target as Node;
		if (!menu?.contains(target) && !button?.contains(target)) hide(false);
	}
</script>

<svelte:window onpointerdown={onWindowPointer} onresize={() => open && hide(false)} />

<button
	bind:this={button}
	type="button"
	class="trigger"
	disabled={off}
	aria-label="Claude runs on: {shown}"
	aria-haspopup="menu"
	aria-expanded={open}
	aria-controls={open ? `${uid}-menu` : undefined}
	title="What Claude runs on"
	onclick={() => (open ? hide() : void show())}
>
	<span>{shown}</span><ChevronDown aria-hidden="true" />
</button>

{#if open}
	<!-- svelte-ignore a11y_interactive_supports_focus -->
	<div
		bind:this={menu}
		id="{uid}-menu"
		class="menu"
		role="menu"
		aria-label="Claude runs on"
		style:left="{left}px"
		style:top="{top}px"
		{onkeydown}
	>
		<div class="head" aria-hidden="true">Claude runs on</div>
		{#each MODE_OPTIONS as option (option.value)}
			<button
				type="button"
				role="menuitemradio"
				class="opt"
				tabindex="-1"
				aria-checked={option.value === mode}
				onclick={() => pick(option.value)}
			>
				<span>{option.label}</span>
				{#if option.value === mode}<Check size={14} aria-hidden="true" />{/if}
			</button>
		{/each}
	</div>
{/if}

<style>
	.trigger {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		flex: none;
		height: 30px;
		padding: 0 6px 0 8px;
		border: 0;
		border-radius: var(--st-radius-sm);
		background: none;
		color: var(--st-mfg);
		font-size: 13px;
		white-space: nowrap;
	}
	.trigger :global(svg.lucide) {
		width: 14px;
		height: 14px;
	}
	.trigger:hover:not(:disabled),
	.trigger[aria-expanded='true'] {
		background: var(--st-hover);
		color: var(--st-fg);
	}
	.trigger:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}
	.trigger:disabled {
		color: var(--st-subtle);
		opacity: 0.7;
	}
	@media (pointer: coarse) {
		.trigger {
			min-height: 44px;
		}
	}
	.menu {
		position: fixed;
		z-index: 90;
		width: 200px;
		padding: 6px;
		border: 1px solid var(--menu-border);
		border-radius: var(--menu-radius);
		background: var(--st-popover);
		box-shadow: var(--menu-shadow);
		color: var(--st-fg);
	}
	.head {
		padding: 8px 8px 4px;
		font-size: 12px;
		color: var(--st-mfg);
	}
	.opt {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 10px;
		width: 100%;
		min-height: var(--menu-row);
		padding: 7px 8px;
		border: 0;
		border-radius: var(--menu-row-radius);
		background: none;
		font-size: 13px;
		text-align: left;
	}
	.opt:focus-visible {
		background: var(--st-accent);
		outline: none;
	}
	@media (hover: hover) {
		.opt:hover {
			background: var(--st-accent);
		}
	}
</style>
