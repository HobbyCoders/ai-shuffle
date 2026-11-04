<script lang="ts">
	/**
	 * "Add account" on a provider: the credential kinds it supports that are not
	 * added yet ("Another API key" where several are allowed). Not shown where
	 * nothing can be added: every kind is added, the viewer may only read, or the
	 * provider is itself a draft. Picking one makes a draft credential.
	 */
	import { tick } from 'svelte';
	import { Plus } from 'lucide-svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import type { AddableKind } from './providers-model';

	interface Props {
		providerName: string;
		kinds: AddableKind[];
		disabled?: boolean;
		onpick: (kind: AddableKind) => void;
	}

	let { providerName, kinds, disabled = false, onpick }: Props = $props();

	const access = getSettingsAccess();
	const uid = $props.id();
	const off = $derived(disabled || access.readOnly || kinds.length === 0);

	let open = $state(false);
	let button: HTMLButtonElement | undefined = $state();
	let menu: HTMLDivElement | undefined = $state();
	let left = $state(0);
	let top = $state(0);

	$effect(() => {
		if (off) open = false;
	});

	async function show() {
		open = true;
		await tick();
		place();
		items()[0]?.focus();
	}

	function hide(refocus = true) {
		open = false;
		if (refocus) button?.focus();
	}

	function items(): HTMLElement[] {
		return Array.from(menu?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
	}

	function place() {
		if (!button || typeof window === 'undefined') return;
		const r = button.getBoundingClientRect();
		const w = menu?.offsetWidth ?? 280;
		const h = menu?.offsetHeight ?? 0;
		left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w));
		const below = r.bottom + 6;
		top = below + h > window.innerHeight - 8 ? Math.max(8, r.top - h - 6) : below;
	}

	function onkeydown(event: KeyboardEvent) {
		const list = items();
		const i = list.indexOf(document.activeElement as HTMLElement);
		if (event.key === 'Escape') {
			event.preventDefault();
			event.stopPropagation();
			hide();
		} else if (event.key === 'ArrowDown') {
			event.preventDefault();
			list[(i + 1) % list.length]?.focus();
		} else if (event.key === 'ArrowUp') {
			event.preventDefault();
			list[(i - 1 + list.length) % list.length]?.focus();
		} else if (event.key === 'Home') {
			event.preventDefault();
			list[0]?.focus();
		} else if (event.key === 'End') {
			event.preventDefault();
			list[list.length - 1]?.focus();
		} else if (event.key === 'Tab') {
			hide(false);
		}
	}

	function pick(kind: AddableKind) {
		hide(false);
		onpick(kind);
	}

	function onWindowPointer(event: PointerEvent) {
		if (!open) return;
		const target = event.target as Node;
		if (menu?.contains(target) || button?.contains(target)) return;
		hide(false);
	}
</script>

<svelte:window onpointerdown={onWindowPointer} onresize={() => open && hide(false)} />

{#if !off}
<button
	bind:this={button}
	type="button"
	class="plus"
	aria-label="Add account to {providerName}"
	aria-haspopup="menu"
	aria-expanded={open}
	aria-controls={open ? `${uid}-menu` : undefined}
	onclick={(e) => {
		e.stopPropagation();
		if (open) hide();
		else void show();
	}}
>
	<Plus /><span>Add account</span>
</button>
{/if}

{#if open}
	<!-- svelte-ignore a11y_interactive_supports_focus -->
	<div
		bind:this={menu}
		id="{uid}-menu"
		class="menu"
		role="menu"
		aria-label="Add to {providerName}"
		style:left="{left}px"
		style:top="{top}px"
		{onkeydown}
	>
		<div class="head" aria-hidden="true">Add to {providerName}</div>
		{#each kinds as kind (kind.menuLabel)}
			<button type="button" role="menuitem" class="opt" tabindex="-1" onclick={() => pick(kind)}>
				<span>{kind.menuLabel}</span>
				<span class="hint">{kind.hint}</span>
			</button>
		{/each}
	</div>
{/if}

<style>
	.plus {
		height: 32px;
		display: inline-flex;
		align-items: center;
		gap: 4px;
		border: 0;
		background: none;
		border-radius: var(--st-radius-sm);
		color: var(--st-fg);
		font-size: 13px;
		font-weight: 500;
		padding: 0 8px;
		white-space: nowrap;
	}
	@media (pointer: coarse) {
		.plus {
			min-height: 44px;
		}
	}
	.plus:hover,
	.plus[aria-expanded='true'] {
		background: var(--st-hover);
		color: var(--st-fg);
	}
	.plus :global(svg.lucide) {
		width: 14px;
		height: 14px;
	}
	.menu {
		position: fixed;
		z-index: 90;
		width: 300px;
		background: var(--st-popover);
		box-shadow: var(--menu-shadow);
		border-radius: var(--menu-radius);
		padding: 6px;
		border: 1px solid var(--menu-border);
	}
	.head {
		font-size: 12px;
		color: var(--st-subtle);
		padding: 8px 8px 4px;
	}
	.opt {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 7px 8px;
		border: 0;
		background: none;
		border-radius: var(--menu-row-radius);
		font-size: 13px;
		text-align: left;
		min-height: var(--menu-row);
	}
	.opt:hover,
	.opt:focus-visible {
		background: var(--st-accent);
		outline: none;
	}
	.hint {
		font-size: 12px;
		color: var(--st-mfg);
		white-space: nowrap;
	}
</style>
