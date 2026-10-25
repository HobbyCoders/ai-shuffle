<script lang="ts">
	/**
	 * TableCopyMenu: the small "Copy" menu above a table in a chat reply.
	 *
	 * AssistantMessage mounts one into each `.md-table` wrapper of a settled
	 * reply (the wrapper comes from chat-markdown.ts). The menu offers the
	 * table as Markdown (from the source the agent wrote, when known) or as
	 * CSV (the rendered text, formula-guarded). It floats to <body> so the
	 * reply's own overflow never clips it.
	 *
	 * Keyboard: Enter, Space or ArrowDown on the button opens the menu on its
	 * first item (a pointer click opens it without moving focus); arrows, Home
	 * and End move; Escape closes and returns focus, however the menu was
	 * opened; Tab closes.
	 */
	import { tick } from 'svelte';
	import { Copy, Check, ChevronDown, FileText, Sheet } from 'lucide-svelte';
	import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
	import { tableClipboardText } from '$lib/utils/table-export';
	import { floatingMenu } from '$lib/components/ui/floating-menu';

	interface Props {
		/** The `.md-table` wrapper this menu belongs to. */
		wrapper: HTMLElement;
	}

	let { wrapper }: Props = $props();

	const uid = $props.id();
	let open = $state(false);
	let status = $state<'idle' | 'copied' | 'error'>('idle');
	let copiedLabel = $state('');
	let trigger = $state<HTMLButtonElement>();
	let menu = $state<HTMLDivElement>();
	let timer: ReturnType<typeof setTimeout> | undefined;

	const ITEMS = [
		{ format: 'markdown', label: 'Copy as Markdown', done: 'Markdown copied' },
		{ format: 'csv', label: 'Copy as CSV', done: 'CSV copied' }
	] as const;

	function items(): HTMLButtonElement[] {
		return menu ? Array.from(menu.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')) : [];
	}

	async function show(focusIndex: number | null = 0) {
		open = true;
		await tick();
		if (focusIndex === null) return;
		const list = items();
		list[(focusIndex + list.length) % list.length]?.focus();
	}

	function hide(refocus = true) {
		if (!open) return;
		open = false;
		if (refocus) trigger?.focus();
	}

	async function copy(format: 'markdown' | 'csv', done: string) {
		const text = tableClipboardText(wrapper, format);
		hide();
		if (!text) return;
		clearTimeout(timer);
		try {
			await copyText(text);
			status = 'copied';
			copiedLabel = done;
		} catch {
			status = 'error';
			copiedLabel = COPY_ERROR_MESSAGE;
		}
		timer = setTimeout(() => {
			status = 'idle';
			copiedLabel = '';
		}, status === 'error' ? 4000 : 1600);
	}

	function closeOnEscape(event: KeyboardEvent) {
		event.preventDefault();
		event.stopPropagation();
		hide();
	}

	function onTriggerKeydown(event: KeyboardEvent) {
		// A pointer click opens the menu with focus left on the button.
		if (event.key === 'Escape' && open) closeOnEscape(event);
		else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
			event.preventDefault();
			void show(event.key === 'ArrowDown' ? 0 : -1);
		}
	}

	function onMenuKeydown(event: KeyboardEvent) {
		const list = items();
		const at = list.indexOf(document.activeElement as HTMLButtonElement);
		let next: number | null = null;
		if (event.key === 'ArrowDown') next = at + 1;
		else if (event.key === 'ArrowUp') next = at - 1;
		else if (event.key === 'Home') next = 0;
		else if (event.key === 'End') next = list.length - 1;
		else if (event.key === 'Escape') {
			closeOnEscape(event);
			return;
		} else if (event.key === 'Tab') {
			hide(false);
			return;
		}
		if (next === null || !list.length) return;
		event.preventDefault();
		list[(next + list.length) % list.length].focus();
	}

	function onWindowPointer(event: PointerEvent) {
		if (!open) return;
		const target = event.target as Node | null;
		if (target && (trigger?.contains(target) || menu?.contains(target))) return;
		hide(false);
	}

	// Some browsers (Safari) do not focus a clicked button, so after a pointer
	// open the key can arrive from anywhere on the page.
	function onWindowKeydown(event: KeyboardEvent) {
		if (open && event.key === 'Escape' && !event.defaultPrevented) closeOnEscape(event);
	}

	$effect(() => () => clearTimeout(timer));
</script>

<svelte:window onpointerdown={onWindowPointer} onkeydown={onWindowKeydown} />

<button
	bind:this={trigger}
	type="button"
	class="tcm-trigger"
	class:copied={status === 'copied'}
	class:failed={status === 'error'}
	aria-haspopup="menu"
	aria-expanded={open}
	aria-controls={open ? `${uid}-menu` : undefined}
	aria-label="Copy table"
	title={status === 'error' ? COPY_ERROR_MESSAGE : 'Copy table'}
	onclick={(event) => (open ? hide() : void show(event.detail === 0 ? 0 : null))}
	onkeydown={onTriggerKeydown}
>
	{#if status === 'copied'}
		<Check size={13} strokeWidth={2.2} aria-hidden="true" />
		<span>Copied</span>
	{:else}
		<Copy size={13} strokeWidth={1.9} aria-hidden="true" />
		<span>{status === 'error' ? 'Copy failed' : 'Copy'}</span>
		<ChevronDown size={12} strokeWidth={2} class="tcm-chev" aria-hidden="true" />
	{/if}
</button>
<span class="tcm-sr" role="status" aria-live="polite">{copiedLabel}</span>

{#if open}
	<div
		bind:this={menu}
		id="{uid}-menu"
		class="tcm-menu"
		role="menu"
		aria-label="Copy table as"
		tabindex="-1"
		use:floatingMenu={{ anchor: trigger, align: 'end', gap: 4 }}
		onkeydown={onMenuKeydown}
	>
		{#each ITEMS as item (item.format)}
			<button type="button" role="menuitem" tabindex="-1" class="tcm-item" onclick={() => void copy(item.format, item.done)}>
				{#if item.format === 'markdown'}
					<FileText size={14} strokeWidth={1.8} aria-hidden="true" />
				{:else}
					<Sheet size={14} strokeWidth={1.8} aria-hidden="true" />
				{/if}
				<span class="tcm-item-label">{item.label}</span>
				<span class="tcm-item-hint" aria-hidden="true">{item.format === 'markdown' ? '.md' : '.csv'}</span>
			</button>
		{/each}
	</div>
{/if}

<style>
	.tcm-trigger {
		position: relative;
		display: inline-flex;
		align-items: center;
		gap: 5px;
		height: 24px;
		padding: 0 7px 0 8px;
		border: 1px solid transparent;
		border-radius: 6px;
		background: transparent;
		color: var(--muted-foreground);
		font: 500 11.5px/1 var(--font-sans);
		letter-spacing: 0.01em;
		cursor: pointer;
		opacity: 0.72;
		transition:
			opacity 140ms ease-out,
			color 140ms ease-out,
			background-color 140ms ease-out,
			border-color 140ms ease-out;
	}
	/* A bigger hit area than the chip it draws: 44px tall on touch. */
	.tcm-trigger::after {
		content: '';
		position: absolute;
		inset: -6px -4px;
	}
	.tcm-trigger:hover,
	.tcm-trigger[aria-expanded='true'] {
		opacity: 1;
		color: var(--foreground);
		background: var(--hover-overlay);
		border-color: var(--border);
	}
	.tcm-trigger:focus-visible {
		opacity: 1;
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}
	.tcm-trigger:active {
		transform: translateY(1px);
	}
	.tcm-trigger.copied {
		opacity: 1;
		color: var(--success);
	}
	.tcm-trigger.failed {
		opacity: 1;
		color: var(--destructive);
	}
	.tcm-trigger :global(.tcm-chev) {
		opacity: 0.7;
		transition: transform 160ms ease-out;
	}
	.tcm-trigger[aria-expanded='true'] :global(.tcm-chev) {
		transform: rotate(180deg);
	}

	.tcm-sr {
		position: absolute;
		width: 1px;
		height: 1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
	}

	.tcm-menu {
		min-width: 188px;
		padding: 4px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--popover);
		color: var(--popover-foreground);
		box-shadow: var(--shadow-l);
		outline: none;
		animation: tcm-in 150ms cubic-bezier(0.2, 0.8, 0.2, 1);
		transform-origin: top right;
	}
	.tcm-menu:global([data-placement='top']) {
		transform-origin: bottom right;
	}
	.tcm-item {
		display: flex;
		align-items: center;
		gap: 9px;
		width: 100%;
		min-height: 32px;
		padding: 0 10px;
		border: 0;
		border-radius: 6px;
		background: transparent;
		color: inherit;
		font: 500 12.5px/1.2 var(--font-sans);
		text-align: left;
		cursor: pointer;
	}
	.tcm-item :global(svg) {
		flex-shrink: 0;
		color: var(--muted-foreground);
	}
	.tcm-item:hover,
	.tcm-item:focus-visible {
		background: var(--hover-overlay);
		outline: none;
	}
	.tcm-item:focus-visible {
		box-shadow: inset 0 0 0 1.5px var(--ring);
	}
	.tcm-item:active {
		background: color-mix(in oklch, var(--primary) 12%, transparent);
	}
	.tcm-item-label {
		flex: 1;
	}
	.tcm-item-hint {
		font: 500 10.5px var(--font-mono);
		color: var(--subtle-foreground, var(--muted-foreground));
	}

	@media (hover: none), (pointer: coarse) {
		.tcm-trigger {
			opacity: 1;
		}
		.tcm-trigger::after {
			inset: -10px -6px;
		}
		.tcm-item {
			min-height: 44px;
			font-size: 14px;
		}
	}

	@keyframes tcm-in {
		from {
			opacity: 0;
			transform: translateY(-3px) scale(0.98);
		}
		to {
			opacity: 1;
			transform: none;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.tcm-menu {
			animation: none;
		}
		.tcm-trigger,
		.tcm-trigger :global(.tcm-chev) {
			transition: none;
		}
	}
</style>
