<script lang="ts">
	/**
	 * SidebarRail — the icon column on the sidebar's left edge.
	 *
	 * New is pinned first and Settings (admins) last; between them sit the
	 * destinations the user put on the rail, in their order (sidebar-prefs).
	 * Everything else waits under More, which the sidebar renders as a flyout
	 * on desktop and a bottom sheet on the phone.
	 *
	 * On the phone this is the drawer's bottom row: labelled tabs (Chats, the
	 * destinations the sidebar picked, Settings, More). New moves to the drawer's
	 * header there, because a tab row is for going places.
	 *
	 * A touch long-press on any destination jumps straight to Customize, so
	 * rearranging the rail never needs a trip through More.
	 */
	import { MessagesSquare, MoreHorizontal, SquarePen } from 'lucide-svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import type { WorkspaceNavItem } from './workspace-items';

	interface Props {
		/** Destinations on the rail, already in the user's order. */
		items: WorkspaceNavItem[];
		/** Pinned at the foot (admins only). */
		settings?: WorkspaceNavItem | null;
		/** "New workspace session" or "New chat", from the mode toggle. */
		newLabel: string;
		moreOpen: boolean;
		/** Something under More is the open page: mark the More button. */
		moreOn?: boolean;
		phone: boolean;
		onNew: () => void;
		onPick: (type: ViewType) => void;
		onPreload?: (type: ViewType) => void;
		onMore: () => void;
		onCustomize: () => void;
		/** The chat list and the chat it leads to are what is on screen. */
		chatsOn?: boolean;
		/** Always there, so the icons below it never jump between screens. */
		onChats?: () => void;
	}

	let {
		items,
		settings = null,
		newLabel,
		moreOpen,
		moreOn = false,
		phone,
		onNew,
		onPick,
		onPreload,
		onMore,
		onCustomize,
		chatsOn = false,
		onChats
	}: Props = $props();

	const iconSize = $derived(phone ? 20 : 18);

	const LONG_PRESS_MS = 520;
	let pressing = $state<string | null>(null);
	let pressTimer: ReturnType<typeof setTimeout> | null = null;
	let pressStart: { x: number; y: number } | null = null;
	let suppressClick = false;

	function clearPress() {
		if (pressTimer) clearTimeout(pressTimer);
		pressTimer = null;
		pressStart = null;
		pressing = null;
	}

	function onPointerDown(e: PointerEvent, id: string) {
		suppressClick = false;
		if (e.pointerType === 'mouse' || !e.isPrimary) return;
		pressing = id;
		pressStart = { x: e.clientX, y: e.clientY };
		pressTimer = setTimeout(() => {
			clearPress();
			suppressClick = true;
			navigator.vibrate?.(10);
			onCustomize();
		}, LONG_PRESS_MS);
	}

	function onPointerMove(e: PointerEvent) {
		if (!pressStart) return;
		if (Math.hypot(e.clientX - pressStart.x, e.clientY - pressStart.y) > 8) clearPress();
	}

	function pick(type: ViewType) {
		if (suppressClick) {
			suppressClick = false;
			return;
		}
		onPick(type);
	}
</script>

<nav class="rail" class:phone aria-label="Workspace">
	{#if !phone}
		<button class="r-btn r-new" type="button" aria-label={newLabel} title={`${newLabel} (Ctrl+N)`} onclick={onNew}>
			<SquarePen size={18} strokeWidth={1.75} />
			<span class="tip" aria-hidden="true">{newLabel}</span>
		</button>
	{/if}
	{#if onChats}
		<button
			class="r-btn"
			class:on={chatsOn}
			type="button"
			aria-label="Chats"
			aria-current={chatsOn ? 'page' : undefined}
			onclick={onChats}
		>
			<span class="r-ic"><MessagesSquare size={iconSize} strokeWidth={1.75} /></span>
			{#if phone}<span class="r-lbl" aria-hidden="true">Chats</span>{/if}
			<span class="tip" aria-hidden="true">Chats</span>
		</button>
	{/if}
	<span class="r-sep" aria-hidden="true"></span>

	{#each items as item (item.id)}
		<button
			class="r-btn"
			class:on={item.on}
			class:pressing={pressing === item.id}
			type="button"
			aria-label={item.label}
			aria-current={item.on ? 'page' : undefined}
			data-rail-item={item.id}
			onclick={() => pick(item.id)}
			onpointerenter={() => onPreload?.(item.id)}
			onfocus={() => onPreload?.(item.id)}
			onpointerdown={(e) => onPointerDown(e, item.id)}
			onpointermove={onPointerMove}
			onpointerup={clearPress}
			onpointercancel={clearPress}
			onpointerleave={clearPress}
			oncontextmenu={(e) => { if (phone) e.preventDefault(); }}
		>
			<span class="r-ic"><item.icon size={iconSize} strokeWidth={1.75} /></span>
			{#if phone}<span class="r-lbl" aria-hidden="true">{item.label}</span>{/if}
			<span class="tip" aria-hidden="true">{item.label}</span>
		</button>
	{/each}

	<span class="grow" aria-hidden="true"></span>

	{#snippet moreButton()}
		<button
			class="r-btn"
			class:on={moreOn && !moreOpen}
			class:open={moreOpen}
			type="button"
			aria-label="More"
			aria-haspopup="menu"
			aria-expanded={moreOpen}
			onclick={onMore}
		>
			<span class="r-ic"><MoreHorizontal size={iconSize} strokeWidth={1.75} /></span>
			{#if phone}<span class="r-lbl" aria-hidden="true">More</span>{/if}
			<span class="tip" aria-hidden="true">More</span>
		</button>
	{/snippet}

	<!-- Desktop keeps Settings at the very foot; on the phone More closes the row. -->
	{#if !phone}{@render moreButton()}{/if}
	{#if settings}
		<button
			class="r-btn"
			class:on={settings.on}
			type="button"
			aria-label={settings.label}
			aria-current={settings.on ? 'page' : undefined}
			onclick={() => onPick(settings.id)}
			onpointerenter={() => onPreload?.(settings.id)}
			onfocus={() => onPreload?.(settings.id)}
		>
			<span class="r-ic"><settings.icon size={iconSize} strokeWidth={1.75} /></span>
			{#if phone}<span class="r-lbl" aria-hidden="true">{settings.label}</span>{/if}
			<span class="tip" aria-hidden="true">{settings.label}</span>
		</button>
	{/if}
	{#if phone}{@render moreButton()}{/if}
</nav>

<style>
	.rail {
		position: relative;
		z-index: 5;
		flex: none;
		width: 56px;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 4px;
		padding: 10px 0 12px;
		background: var(--sidebar);
	}
	/* On a phone this is not a desktop rail: it is the drawer's bottom row of
	   labelled tabs, where a thumb already is. */
	.rail.phone {
		width: 100%;
		flex-direction: row;
		gap: 0;
		padding: 4px 4px calc(4px + env(safe-area-inset-bottom, 0px));
		border-top: 1px solid var(--border-subtle);
	}
	.rail.phone .r-sep,
	.rail.phone .grow { display: none; }

	.r-btn {
		position: relative;
		flex: none;
		width: 36px;
		height: 36px;
		display: grid;
		place-items: center;
		padding: 0;
		border: 0;
		border-radius: var(--radius-control);
		background: transparent;
		color: var(--sidebar-muted);
		cursor: pointer;
		transition: background-color 0.13s ease, color 0.13s ease, transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
		-webkit-tap-highlight-color: transparent;
		-webkit-touch-callout: none;
		user-select: none;
	}
	.r-ic { display: grid; place-items: center; }
	.r-btn:hover { background: var(--row-hover); color: var(--sidebar-foreground); }
	.r-btn.on,
	.r-btn.open { background: var(--row-selected); color: var(--sidebar-foreground); }
	/* Five equal tabs: the selected one gets a pill behind its icon and a heavier
	   label, so it never relies on colour alone. */
	.phone .r-btn {
		flex: 1 1 0;
		min-width: 0;
		width: auto;
		height: 56px;
		grid-template-rows: 28px 16px;
		align-content: center;
		justify-items: center;
		gap: 2px;
		background: transparent;
	}
	.phone .r-ic {
		width: 48px;
		height: 28px;
		border-radius: 14px;
		transition: background-color 0.13s ease;
	}
	.phone .r-btn.on .r-ic,
	.phone .r-btn.open .r-ic { background: var(--row-selected); }
	.r-lbl {
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 11.5px;
		font-weight: 500;
		line-height: 16px;
	}
	.phone .r-btn.on .r-lbl,
	.phone .r-btn.open .r-lbl { font-weight: 600; }
	.r-btn.pressing { transform: scale(0.92); }
	.r-btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }

	.r-new { color: var(--sidebar-foreground); }

	.r-sep { flex: none; width: 24px; height: 1px; margin: 4px 0; background: var(--sidebar-border); }
	.grow { flex: 1 0 8px; }

	/* Desktop tooltips, beside the button. They wait long enough not to flash
	   over the session list on the way past, and leave at once. */
	.tip {
		position: absolute;
		left: calc(100% + 6px);
		top: 50%;
		transform: translateY(-50%);
		z-index: 9;
		white-space: nowrap;
		padding: 4px 8px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--popover);
		color: var(--foreground);
		font-size: var(--fs-label);
		box-shadow: var(--shadow-m);
		opacity: 0;
		pointer-events: none;
	}
	.rail:not(.phone) .r-btn:hover .tip,
	.rail:not(.phone) .r-btn:focus-visible .tip { opacity: 1; transition: opacity 0.1s ease 0.45s; }
	.rail:not(.phone) .r-btn:active .tip { opacity: 0; transition: none; }
	.phone .tip { display: none; }

	@media (hover: none) {
		.tip { display: none; }
	}

	@media (prefers-reduced-motion: reduce) {
		.r-btn, .tip { transition: none; }
	}
</style>
