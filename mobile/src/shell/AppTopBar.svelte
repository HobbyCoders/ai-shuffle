<script lang="ts">
	/**
	 * AppTopBar - compact mobile app bar + navigation drawer.
	 *
	 * Rendered by MobileAppLayout; the mobile shell is selected by build
	 * (VITE_SHUFFLE_SHELL), never a viewport width, so a narrow desktop browser
	 * keeps the desktop shell. The bar carries ☰ menu + current-page
	 * title, and every navigation destination lives in the slide-in drawer,
	 * which hosts the same Sidebar component the desktop shell shows beside the
	 * main panel.
	 *
	 * The notification bell moved into that sidebar's header, so on this bar it
	 * is represented by a dot on the ☰ while the drawer is closed. Unread state
	 * therefore stays visible without a second bell, and opening the drawer puts
	 * the real bell one row away.
	 *
	 * On desktop the sidebar itself replaces this bar's old tab row, and the main
	 * panel's StudioTopBar carries the breadcrumbs and actions.
	 */
	import { tick, untrack } from 'svelte';
	import { pushState } from '$app/navigation';
	import { page } from '$app/state';
	import { Menu, Mic, X } from 'lucide-svelte';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences.svelte';
	import { globalSttState, triggerToggle } from '$lib/services/globalStt';
	import { activeView } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { unreadCount } from '$lib/stores/notifications';
	import Sidebar from '$lib/components/layout/Sidebar.svelte';
	import SessionBreadcrumbs from '$lib/components/layout/SessionBreadcrumbs.svelte';
	import { projects } from '$lib/stores/app-data.svelte';
	import { chatHeaderTitle } from '$lib/components/layout/chat-empty-state';
	import { canStartDrawerSwipe, completes, dragOffset, lockAxis, type DrawerSwipe } from './drawer-gesture';

	interface Props {
		isAdmin?: boolean;
		onNewSession: () => void;
		onSettledOpenSession: () => void;
		onOpenSession: (sessionId: string) => void;
		onOpenFeature: (type: ViewType) => void;
		onOpenSpotlight?: () => void;
		hideGlobalStt?: boolean;
	}

	let {
		isAdmin = false,
		onNewSession,
		onSettledOpenSession,
		onOpenSession,
		onOpenFeature,
		onOpenSpotlight,
		hideGlobalStt = false
	}: Props = $props();

	const BUILD_MEMBERS: ViewType[] = ['build', 'profile'];
	const currentType = $derived($activeView?.type ?? null);
	const buildActive = $derived(currentType ? BUILD_MEMBERS.includes(currentType) : false);

	// Current-page title shown beside the ☰ on the mobile bar.
	const TITLES: Record<string, string> = {
		assistant: 'Assistant',
		'file-browser': 'Files',
		notes: 'Notes',
		automations: 'Automations',
		project: 'Projects',
		docs: 'Docs',
		stats: 'Stats',
		settings: 'Settings',
		// Plugins, MCP servers and Hooks all live in Settings.
		plugins: 'Settings',
		mcp: 'Settings',
		hooks: 'Settings'
	};
	// In a chat, the mobile bar shows the chat name (the old PageHeader title row
	// is dropped on mobile, so the name lives here, centered). Mirrors MainPanel's
	// headerTitle derivation.
	const boundChatId = $derived(
		currentType === 'session' ? ($activeSession.sessionId ?? null) : null
	);
	const chatTitle = $derived.by(() => {
		const s = $activeSession;
		return chatHeaderTitle({
			sessionId: s?.sessionId,
			title: s?.title,
			messageCount: s?.messages?.length ?? 0
		});
	});
	const mobileTitle = $derived.by(() => {
		if (currentType === 'session') return chatTitle;
		if (currentType === 'board') return 'Sessions';
		if (buildActive) return 'Build';
		return (currentType && TITLES[currentType]) || 'AI Shuffle';
	});

	// --- Navigation drawer ------------------------------------------------------
	// The drawer mounts on first use and then stays mounted (inert while
	// closed), so reopening it is instant and keeps the list where it was.
	let drawerOpen = $state(false);
	let mounted = $state(false);
	let hamburgerRef = $state<HTMLButtonElement | null>(null);
	let drawerRef = $state<HTMLElement | null>(null);

	// Opening moves focus into the drawer (it covers the whole screen), and a
	// dismissal hands it back to the ☰ instead of dropping it on <body>.
	async function openDrawer() {
		mounted = true;
		if (!drawerOpen) {
			drawerOpen = true;
			pushDrawerEntry();
		}
		await tick();
		drawerRef?.querySelector<HTMLElement>('.drawer-close')?.focus({ preventScroll: true });
	}

	function closeDrawer() {
		const focusWasInside = !!drawerRef?.contains(document.activeElement);
		drawerOpen = false;
		if (focusWasInside) hamburgerRef?.focus({ preventScroll: true });
	}

	function dismissDrawer() {
		if (!drawerOpen) return;
		closeDrawer();
		popDrawerEntry();
	}

	// --- Android Back ---------------------------------------------------------
	// Opening pushes one shallow history entry, so the system Back closes the
	// drawer instead of leaving the app. Closing it any other way pops that
	// entry again, so Back never needs a second press to do something.
	let entryLive = false;

	function pushDrawerEntry() {
		try {
			pushState('', { ...(page.state ?? {}), mobileDrawer: true });
		} catch {
			// Router not ready (early boot, tests): Back just won't close it.
		}
	}

	function popDrawerEntry() {
		if (!entryLive) return;
		entryLive = false;
		history.back();
	}

	$effect(() => {
		const inHistory = !!page.state?.mobileDrawer;
		untrack(() => {
			if (inHistory) {
				entryLive = true;
			} else if (entryLive) {
				// Back popped our entry: close without popping a second one.
				entryLive = false;
				if (drawerOpen) closeDrawer();
			}
		});
	});

	function handleWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && !e.defaultPrevented) dismissDrawer();
	}

	// Navigation that does not go through the Sidebar's callbacks — a tapped
	// notification in the drawer's bell, a system notification — arrives as the
	// shared `open-session` event. The full-screen drawer must not stay over it.
	// Focus goes back to the ☰ like any dismissal; the opened view may take it.
	$effect(() => {
		const close = () => dismissDrawer();
		window.addEventListener('open-session', close);
		return () => window.removeEventListener('open-session', close);
	});

	// Run a drawer action and dismiss the drawer in one step. The dismissal
	// comes first, so an action that moves focus (into a chat) still wins.
	// The drawer then slides away over the view the action opened.
	function drawerGo<T extends unknown[]>(action: (...args: T) => void) {
		return (...args: T) => {
			dismissDrawer();
			action(...args);
		};
	}

	// --- Swipe ----------------------------------------------------------------
	// A right swipe on the main screen pulls the drawer open; a left swipe
	// inside it pushes it closed. The drawer follows the finger, and on release
	// it finishes the move if it went far enough or was flicked.
	interface Swipe {
		kind: DrawerSwipe;
		id: number;
		x0: number;
		y0: number;
		locked: boolean;
		lastX: number;
		lastT: number;
		velocity: number;
	}
	let swipe: Swipe | null = null;
	/** The drawer's offset while a finger drags it; null otherwise. */
	let dragX = $state<number | null>(null);
	let viewportW = $state(typeof window === 'undefined' ? 400 : window.innerWidth);

	function touchStart(e: TouchEvent) {
		if (e.touches.length !== 1) { endSwipe(false); return; }
		if (swipe) return;
		const touch = e.touches[0];
		const target = e.target;
		let kind: DrawerSwipe = 'open';
		if (drawerOpen) {
			if (!(target instanceof Node) || !drawerRef?.contains(target)) return;
			// A sheet or Customize over the list owns its own gestures.
			if (drawerRef.querySelector('.sheet.open, .cust')) return;
			kind = 'close';
		}
		if (!canStartDrawerSwipe(target, kind)) return;
		viewportW = window.innerWidth || viewportW;
		swipe = {
			kind,
			id: touch.identifier,
			x0: touch.clientX,
			y0: touch.clientY,
			locked: false,
			lastX: touch.clientX,
			lastT: e.timeStamp || performance.now(),
			velocity: 0
		};
	}

	function touchMove(e: TouchEvent) {
		const current = swipe;
		if (!current) return;
		const touch = Array.from(e.touches).find((t) => t.identifier === current.id);
		if (!touch || e.touches.length !== 1) { endSwipe(false); return; }
		const dx = touch.clientX - current.x0;
		if (!current.locked) {
			const lock = lockAxis(dx, touch.clientY - current.y0, current.kind);
			if (lock === null) return;
			if (!lock || !e.cancelable) { swipe = null; return; }
			current.locked = true;
			mounted = true;
		}
		e.preventDefault();
		const now = e.timeStamp || performance.now();
		const dt = now - current.lastT;
		if (dt > 0) current.velocity = 0.7 * ((touch.clientX - current.lastX) / dt) + 0.3 * current.velocity;
		current.lastX = touch.clientX;
		current.lastT = now;
		dragX = dragOffset(dx, viewportW, current.kind);
	}

	function touchEnd(e: TouchEvent) {
		if (!swipe) return;
		endSwipe(e.touches.length === 0);
	}

	function endSwipe(release: boolean) {
		const current = swipe;
		swipe = null;
		if (!current?.locked) return;
		const done = release && dragX !== null && completes(dragX, current.velocity, viewportW, current.kind);
		dragX = null;
		if (!done) return;
		if (current.kind === 'open') void openDrawer();
		else dismissDrawer();
	}

	$effect(() => {
		const cancel = () => endSwipe(false);
		window.addEventListener('touchstart', touchStart, { capture: true, passive: true });
		window.addEventListener('touchmove', touchMove, { passive: false });
		window.addEventListener('touchend', touchEnd);
		window.addEventListener('touchcancel', cancel);
		return () => {
			window.removeEventListener('touchstart', touchStart, true);
			window.removeEventListener('touchmove', touchMove);
			window.removeEventListener('touchend', touchEnd);
			window.removeEventListener('touchcancel', cancel);
		};
	});

	/** Where the drawer sits right now, as a translateX value. */
	const drawerShift = $derived(
		dragX === null
			? drawerOpen ? '0px' : '-100%'
			: drawerOpen ? `${dragX}px` : `calc(-100% + ${dragX}px)`
	);
	/** 0 closed, 1 open: the scrim fades with the drawer. */
	const drawerProgress = $derived(
		dragX === null
			? drawerOpen ? 1 : 0
			: drawerOpen ? 1 + dragX / viewportW : dragX / viewportW
	);
</script>

<svelte:window onkeydown={handleWindowKeydown} />

<header class="topbar topbar-mobile">
	<button
		bind:this={hamburgerRef}
		class="tb-hamburger"
		onclick={openDrawer}
		aria-controls="mobile-nav-drawer"
		aria-label={$unreadCount > 0 ? `Open menu (${$unreadCount} unread notifications)` : 'Open menu'}
		aria-expanded={drawerOpen}
	>
		<Menu size={20} strokeWidth={1.8} />
		{#if !drawerOpen && $unreadCount > 0}
			<span class="tb-unread" aria-hidden="true"></span>
		{/if}
	</button>

	{#if boundChatId}
		<SessionBreadcrumbs title={mobileTitle} sessionId={boundChatId}
			projectLabel={$projects.find(p => p.id === $activeSession.project)?.name || 'Sessions'} mobile />
	{:else}
		<span class="tb-context-title">{mobileTitle}</span>
	{/if}

	<div class="tb-actions">
		{#if $hotkeyPreferences.globalSttEnabled && !hideGlobalStt}
			<button class="tb-hamburger" class:recording={$globalSttState.isRecording}
				onclick={triggerToggle}
				aria-label={$globalSttState.isRecording ? 'Stop recording' : 'Start voice input'}>
				<Mic size={18} />
			</button>
		{/if}
	</div>
</header>

<!-- Slide-in navigation drawer: hosts the full sidebar. Mounted on first
     use, then kept (inert while closed) so it reopens where it was. -->
{#if mounted}
	{#if drawerOpen || dragX !== null}
		<div
			class="drawer-overlay"
			role="button"
			tabindex="-1"
			aria-label="Close menu"
			style:opacity={drawerProgress}
			onclick={dismissDrawer}
			onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') dismissDrawer(); }}
		></div>
	{/if}
	<aside
		id="mobile-nav-drawer"
		class="drawer"
		class:open={drawerOpen}
		class:dragging={dragX !== null}
		aria-label="Navigation"
		aria-hidden={drawerOpen || dragX !== null ? undefined : 'true'}
		inert={!drawerOpen}
		style:transform="translateX({drawerShift})"
		bind:this={drawerRef}
	>
		<Sidebar
			{isAdmin}
			onNewSession={drawerGo(onNewSession)}
			onSettledOpenSession={drawerGo(onSettledOpenSession)}
			onOpenSession={drawerGo(onOpenSession)}
			onOpenFeature={drawerGo(onOpenFeature)}
			onOpenSpotlight={onOpenSpotlight ? drawerGo(onOpenSpotlight) : undefined}
		>
			{#snippet headerAction()}
				<button class="drawer-close" onclick={dismissDrawer} aria-label="Close menu">
					<X size={20} strokeWidth={1.8} />
				</button>
			{/snippet}
		</Sidebar>
	</aside>
{/if}

<style>
	.topbar {
		display: flex;
		align-items: center;
		gap: 4px;
		height: 52px;
		flex-shrink: 0;
		padding: 0 6px 0 4px;
		background: var(--background);
		border-bottom: 1px solid var(--border);
		position: relative;
		z-index: 40;
	}

	.tb-hamburger {
		position: relative;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		flex-shrink: 0;
		background: transparent;
		border: none;
		border-radius: 8px;
		color: var(--foreground);
		cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
		-webkit-tap-highlight-color: transparent;
	}
	.tb-hamburger:hover { color: var(--foreground); background: var(--accent); }

	/* A dot, not a count: the exact number is one tap away on the bell inside the
	   drawer, and a badge on a navigation control reads as a nav badge. */
	.tb-unread {
		position: absolute;
		top: 7px;
		right: 7px;
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: var(--destructive);
		box-shadow: 0 0 0 2px var(--background);
		pointer-events: none;
	}

	/* Never put popovers in an overflow rail: unfolded phones are wider than
	   the phone breakpoint and their anchored dialogs would be clipped. */
	.tb-actions {
		display: flex;
		align-items: center;
		gap: 4px;
		flex: none;
		margin-left: auto;
	}
	.recording { color: var(--destructive); }

	/* Chat actions live below this bar, leaving room for the title. */
	.tb-context-title {
		flex: 1 1 auto;
		min-width: 0;
		text-align: center;
		padding: 0 4px;
		font-size: 14px;
		font-weight: 600;
		letter-spacing: -0.01em;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	/* Drawer */
	.drawer-overlay {
		position: fixed;
		inset: 0;
		background: color-mix(in oklch, var(--background) 55%, transparent);
		backdrop-filter: blur(2px);
		-webkit-backdrop-filter: blur(2px);
		z-index: 90;
		transition: opacity 200ms ease;
	}

	.drawer {
		position: fixed;
		top: 0;
		left: 0;
		right: 0;
		bottom: 0;
		width: 100%;
		display: flex;
		flex-direction: column;
		background: var(--sidebar);
		border-right: none;
		box-shadow: none;
		z-index: 100;
		overflow: hidden;
		touch-action: pan-y;
		will-change: transform;
		/* Closing slides out over the view it opened; visibility flips at the
		   end so a closed drawer is never painted or hit-tested. */
		visibility: hidden;
		transition:
			transform 220ms cubic-bezier(0.16, 1, 0.3, 1),
			visibility 0s linear 220ms;
	}
	.drawer.open,
	.drawer.dragging {
		visibility: visible;
		transition: transform 220ms cubic-bezier(0.16, 1, 0.3, 1), visibility 0s;
	}
	.drawer.dragging { transition: none; }

	.drawer-close {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 40px;
		height: 40px;
		flex: none;
		background: transparent;
		border: none;
		border-radius: 8px;
		color: var(--sidebar-muted);
		cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.drawer-close:hover { color: var(--foreground); background: var(--sidebar-accent); }


	:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	@media (prefers-reduced-motion: reduce) {
		.drawer,
		.drawer.open,
		.drawer-overlay { transition: none; }
	}
</style>
