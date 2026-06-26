<script lang="ts">
	/**
	 * AppTopBar - Global navigation bar
	 *
	 * Desktop: pages are siblings reached from one left-aligned row beside the
	 * brand — Sessions · Files · Notes · Build · Automations · Projects — with a
	 * Build-style 2px underline on the active tab, plus search / new-session /
	 * account on the right.
	 *
	 * Mobile (isMobile, width ≤ 1080px — phones AND unfolded foldables): the row
	 * of tabs is replaced by a decluttered bar — ☰ menu + current-page title +
	 * search + new-session — and all navigation moves into a slide-in drawer
	 * (Option A from the redesign mockup). The drawer holds the labeled
	 * destinations, the secondary Docs/Stats/Settings group, and the signed-in
	 * account footer.
	 *
	 * Styled entirely with the app's existing theme tokens (no bespoke palette).
	 */
	import {
		Files,
		StickyNote,
		Hammer,
		CalendarClock,
		Search,
		Plus,
		CircleUser,
		Settings,
		BarChart3,
		BookOpen,
		FolderKanban,
		MessagesSquare,
		Menu,
		X
	} from 'lucide-svelte';
	import { activeView } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import { username } from '$lib/stores/auth.svelte';
	import { activeSession } from '$lib/stores/active-session';

	interface Props {
		isAdmin?: boolean;
		isMobile?: boolean;
		onNewSession: () => void;
		onOpenSessions: () => void;
		onOpenFeature: (type: ViewType) => void;
		onOpenSpotlight?: () => void;
	}

	let {
		isAdmin = false,
		isMobile = false,
		onNewSession,
		onOpenSessions,
		onOpenFeature,
		onOpenSpotlight
	}: Props = $props();

	// Build group members — the agent-config tools live inside the Build page.
	// Listed so the "Build" tab highlights when a member view (deep-link) is active.
	const BUILD_MEMBERS: ViewType[] = ['build', 'profile', 'subagent', 'mcp', 'plugins', 'hooks'];

	// "Account & more" dropdown — Projects now lives in the main nav row.
	const overflowItems: { type: ViewType; label: string; icon: typeof Settings; adminOnly?: boolean }[] = [
		{ type: 'docs', label: 'Docs', icon: BookOpen },
		{ type: 'stats', label: 'Stats', icon: BarChart3 },
		{ type: 'settings', label: 'Settings', icon: Settings, adminOnly: true }
	];

	const filteredOverflow = $derived(overflowItems.filter((i) => !i.adminOnly || isAdmin));

	const currentType = $derived($activeView?.type ?? null);
	const sessionsActive = $derived(currentType === 'chat' || currentType === 'board');
	const buildActive = $derived(currentType ? BUILD_MEMBERS.includes(currentType) : false);

	function isActive(t: ViewType): boolean {
		return currentType === t;
	}

	// Primary destinations — shared by the desktop tab row and the mobile drawer
	// so the two never drift. `active` mirrors the bespoke desktop highlighting.
	const navItems = $derived([
		{ label: 'Sessions', icon: MessagesSquare, active: sessionsActive, action: onOpenSessions },
		{ label: 'Files', icon: Files, active: isActive('file-browser'), action: () => onOpenFeature('file-browser') },
		{ label: 'Notes', icon: StickyNote, active: isActive('notes'), action: () => onOpenFeature('notes') },
		{ label: 'Build', icon: Hammer, active: buildActive, action: () => onOpenFeature('build') },
		...(isAdmin
			? [{ label: 'Automations', icon: CalendarClock, active: isActive('automations'), action: () => onOpenFeature('automations') }]
			: []),
		{ label: 'Projects', icon: FolderKanban, active: isActive('project'), action: () => onOpenFeature('project') }
	]);

	// Current-page title shown beside the ☰ on the mobile bar.
	const TITLES: Record<string, string> = {
		'file-browser': 'Files',
		notes: 'Notes',
		automations: 'Automations',
		project: 'Projects',
		docs: 'Docs',
		stats: 'Stats',
		settings: 'Settings'
	};
	// In a chat, the mobile bar shows the chat name (the old PageHeader title row
	// is dropped on mobile, so the name lives here, centered). Mirrors MainPanel's
	// headerTitle derivation.
	const chatTitle = $derived.by(() => {
		const s = $activeSession;
		if (s && s.messages.length > 0) return s.title || 'Untitled';
		return 'New Chat';
	});
	const mobileTitle = $derived.by(() => {
		if (currentType === 'chat') return chatTitle;
		if (currentType === 'board') return 'Sessions';
		if (buildActive) return 'Build';
		return (currentType && TITLES[currentType]) || 'ai-shuffle';
	});

	// Overflow ("Account & more") dropdown state (desktop)
	let overflowOpen = $state(false);
	let overflowDropdownEl = $state<HTMLDivElement | null>(null);

	// Mobile navigation drawer state
	let drawerOpen = $state(false);

	// Close on outside click (robust: ignore clicks inside the open menu/trigger).
	function handleWindowClick(e: MouseEvent) {
		const t = e.target as Node;
		if (overflowOpen && overflowDropdownEl && !overflowDropdownEl.contains(t)) overflowOpen = false;
	}

	function handleWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			overflowOpen = false;
			drawerOpen = false;
		}
	}

	function pick(type: ViewType) {
		overflowOpen = false;
		onOpenFeature(type);
	}

	// Run a drawer action and dismiss the drawer in one step.
	function drawerGo(action: () => void) {
		drawerOpen = false;
		action();
	}

	function openSearch() {
		drawerOpen = false;
		onOpenSpotlight?.();
	}
</script>

<svelte:window onclick={handleWindowClick} onkeydown={handleWindowKeydown} />

{#if isMobile}
	<!-- Mobile: decluttered bar — ☰ menu + centered context/chat title + new session.
	     Search lives in the drawer, so it's intentionally absent from the bar. -->
	<header class="topbar topbar-mobile">
		<button class="tb-hamburger" onclick={() => (drawerOpen = true)} aria-label="Open menu" aria-expanded={drawerOpen}>
			<Menu size={20} strokeWidth={1.8} />
		</button>

		<span class="tb-context-title">{mobileTitle}</span>

		<button class="tb-new-icon" onclick={onNewSession} aria-label="New session (Cmd/Ctrl+N)" title="New session">
			<Plus size={18} strokeWidth={2.2} />
		</button>
	</header>

	<!-- Slide-in navigation drawer -->
	{#if drawerOpen}
		<div
			class="drawer-overlay"
			role="button"
			tabindex="-1"
			aria-label="Close menu"
			onclick={() => (drawerOpen = false)}
			onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') drawerOpen = false; }}
		></div>
		<aside class="drawer" aria-label="Navigation">
			<div class="drawer-head">
				<div class="brand">
					<span class="brand-mark" aria-hidden="true">
						<svg width="22" height="22" viewBox="0 0 40 40" fill="none">
							<path d="M20 4C11.16 4 4 11.16 4 20s7.16 16 16 16 16-7.16 16-16S28.84 4 20 4zm0 28c-6.63 0-12-5.37-12-12S13.37 8 20 8s12 5.37 12 12-5.37 12-12 12z" fill="currentColor" opacity="0.25"/>
							<path d="M15 17c0-1.1.9-2 2-2h6c1.1 0 2 .9 2 2v6c0 1.1-.9 2-2 2h-6c-1.1 0-2-.9-2-2v-6z" fill="currentColor" opacity="0.55"/>
							<circle cx="20" cy="20" r="3" fill="currentColor"/>
						</svg>
					</span>
					<span class="brand-name">ai-shuffle</span>
				</div>
				<button class="drawer-close" onclick={() => (drawerOpen = false)} aria-label="Close menu">
					<X size={18} strokeWidth={1.8} />
				</button>
			</div>

			{#if onOpenSpotlight}
				<button class="drawer-search" onclick={openSearch}>
					<Search size={16} strokeWidth={1.7} />
					<span>Search…</span>
					<kbd class="tb-kbd">⌘K</kbd>
				</button>
			{/if}

			<nav class="drawer-nav" aria-label="Pages">
				{#each navItems as item}
					<button class="drawer-item" class:active={item.active} onclick={() => drawerGo(item.action)}>
						<item.icon size={18} strokeWidth={1.6} /><span>{item.label}</span>
					</button>
				{/each}
			</nav>

			<div class="drawer-divider"></div>

			<nav class="drawer-nav" aria-label="More">
				{#each filteredOverflow as item}
					<button class="drawer-item" class:active={isActive(item.type)} onclick={() => drawerGo(() => onOpenFeature(item.type))}>
						<item.icon size={18} strokeWidth={1.6} /><span>{item.label}</span>
					</button>
				{/each}
			</nav>

			{#if $username}
				<div class="drawer-foot">
					<span class="drawer-avatar"><CircleUser size={22} strokeWidth={1.5} /></span>
					<div class="drawer-user">
						<span class="drawer-user-name">{$username}</span>
						<span class="drawer-user-sub">Signed in</span>
					</div>
				</div>
			{/if}
		</aside>
	{/if}
{:else}
	<!-- Desktop: full nav row -->
	<header class="topbar">
		<!-- Brand -->
		<div class="brand">
			<span class="brand-mark" aria-hidden="true">
				<svg width="22" height="22" viewBox="0 0 40 40" fill="none">
					<path d="M20 4C11.16 4 4 11.16 4 20s7.16 16 16 16 16-7.16 16-16S28.84 4 20 4zm0 28c-6.63 0-12-5.37-12-12S13.37 8 20 8s12 5.37 12 12-5.37 12-12 12z" fill="currentColor" opacity="0.25"/>
					<path d="M15 17c0-1.1.9-2 2-2h6c1.1 0 2 .9 2 2v6c0 1.1-.9 2-2 2h-6c-1.1 0-2-.9-2-2v-6z" fill="currentColor" opacity="0.55"/>
					<circle cx="20" cy="20" r="3" fill="currentColor"/>
				</svg>
			</span>
			<span class="brand-name">ai-shuffle</span>
		</div>

		<!-- Primary page navigation — left-aligned, Build-style underline on active -->
		<nav class="tb-nav" aria-label="Workspace">
			{#each navItems as item}
				<button class="tb-tab" class:active={item.active} onclick={item.action}>
					<item.icon size={16} strokeWidth={1.6} /><span>{item.label}</span>
				</button>
			{/each}
		</nav>

		<div class="tb-spacer"></div>

		<!-- Right: actions -->
		<div class="tb-right">
			{#if onOpenSpotlight}
				<button class="tb-search" onclick={onOpenSpotlight} title="Search (Cmd/Ctrl+K)">
					<Search size={15} strokeWidth={1.6} />
					<span class="tb-search-label">Search</span>
					<kbd class="tb-kbd">⌘K</kbd>
				</button>
			{/if}

			<button class="tb-new" onclick={onNewSession} title="New session (Cmd/Ctrl+N)">
				<Plus size={16} strokeWidth={2} /><span class="tb-new-label">New session</span>
			</button>

			<div class="tb-dropdown" bind:this={overflowDropdownEl}>
				<button
					class="tb-user"
					aria-haspopup="true"
					aria-expanded={overflowOpen}
					title="Account & more"
					aria-label="Account & more"
					onclick={() => { overflowOpen = !overflowOpen; }}
				>
					<CircleUser size={20} strokeWidth={1.6} />
				</button>
				{#if overflowOpen}
					<div class="tb-menu tb-menu-right" role="menu">
						{#each filteredOverflow as item}
							<button class="tb-menu-item" class:active={isActive(item.type)} role="menuitem" onclick={() => pick(item.type)}>
								<item.icon size={15} strokeWidth={1.6} /><span>{item.label}</span>
							</button>
						{/each}
					</div>
				{/if}
			</div>
		</div>
	</header>
{/if}

<style>
	.topbar {
		display: flex;
		align-items: center;
		gap: 6px;
		height: 52px;
		flex-shrink: 0;
		padding: 0 12px;
		background: color-mix(in oklch, var(--muted) 40%, var(--background));
		border-bottom: 1px solid var(--border);
		position: relative;
		z-index: 40;
	}

	.tb-spacer { flex: 1 1 auto; }
	.tb-right {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-shrink: 0;
	}

	/* Brand */
	.brand {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-shrink: 0;
		padding-right: 10px;
	}
	.brand-mark { color: var(--primary); display: flex; }
	.brand-name {
		font-family: var(--font-display, Georgia, serif);
		font-size: 0.95rem;
		font-weight: 700;
		letter-spacing: -0.01em;
		color: var(--foreground);
	}

	/* Nav — left-aligned, full-height tabs with a Build-style underline on active */
	.tb-nav {
		display: flex;
		align-items: stretch;
		align-self: stretch;
		gap: 2px;
	}

	.tb-tab {
		position: relative;
		display: inline-flex;
		align-items: center;
		gap: 7px;
		padding: 0 13px;
		background: transparent;
		border: none;
		color: var(--muted-foreground);
		font-size: 0.8125rem;
		font-weight: 500;
		white-space: nowrap;
		cursor: pointer;
		transition: color 120ms ease;
	}
	.tb-tab:hover { color: var(--foreground); }
	.tb-tab.active { color: var(--foreground); }
	.tb-tab.active::after {
		content: '';
		position: absolute;
		left: 11px;
		right: 11px;
		bottom: 0;
		height: 2px;
		border-radius: 2px 2px 0 0;
		background: var(--primary);
	}
	.tb-tab :global(svg) { flex-shrink: 0; opacity: 0.9; }

	/* Dropdown */
	.tb-dropdown { position: relative; }
	.tb-menu {
		position: absolute;
		top: calc(100% + 6px);
		left: 0;
		min-width: 220px;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		box-shadow: var(--shadow-l);
		padding: 6px;
		z-index: 60;
		animation: tb-pop 140ms cubic-bezier(0.16, 1, 0.3, 1);
	}
	.tb-menu-right { left: auto; right: 0; }
	.tb-menu-item {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 8px 10px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) - 4px);
		color: var(--foreground);
		font-size: 0.8125rem;
		font-weight: 500;
		text-align: left;
		cursor: pointer;
		transition: background-color 120ms ease, color 120ms ease;
	}
	.tb-menu-item:hover { background: var(--hover-overlay); }
	.tb-menu-item.active { color: var(--selection-text); background: var(--selection-bg-emphasis); }
	.tb-menu-item :global(svg) { color: var(--muted-foreground); flex-shrink: 0; }
	.tb-menu-item.active :global(svg) { color: var(--selection-text); }

	@keyframes tb-pop {
		from { opacity: 0; transform: translateY(-4px) scale(0.98); }
		to { opacity: 1; transform: translateY(0) scale(1); }
	}

	/* Search */
	.tb-search {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		height: 34px;
		padding: 0 10px;
		background: var(--input-bg);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		color: var(--muted-foreground);
		font-size: 0.8125rem;
		cursor: pointer;
		transition: border-color 120ms ease, color 120ms ease;
	}
	.tb-search:hover { color: var(--foreground); border-color: var(--input-border-hover); }
	.tb-kbd {
		font-family: var(--font-mono, monospace);
		font-size: 0.625rem;
		padding: 1px 5px;
		border: 1px solid var(--border);
		border-radius: 4px;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted) 50%, transparent);
	}

	/* New session */
	.tb-new {
		display: inline-flex;
		align-items: center;
		gap: 7px;
		height: 34px;
		padding: 0 13px;
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		font-size: 0.8125rem;
		font-weight: 600;
		cursor: pointer;
		flex-shrink: 0;
		transition: filter 120ms ease;
	}
	.tb-new:hover { filter: brightness(1.08); }

	/* User / account button (replaces the old three-dot "More") */
	.tb-user {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 34px;
		height: 34px;
		flex-shrink: 0;
		background: transparent;
		border: none;
		border-radius: 50%;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.tb-user:hover { color: var(--foreground); background: var(--hover-overlay); }

	:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	/* ===================================================================
	   Mobile bar (rendered when isMobile) — ☰ + centered title + new.
	   isMobile is width-based (≤1080px) and owned by AppLayout, so this
	   markup only mounts on phones / unfolded foldables / narrow desktop
	   windows; no media query needed to gate it.
	   =================================================================== */
	.topbar-mobile {
		height: 52px;
		padding: 0 6px 0 4px;
		gap: 4px;
	}

	.tb-hamburger {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 40px;
		height: 40px;
		flex-shrink: 0;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		color: var(--foreground);
		cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
		-webkit-tap-highlight-color: transparent;
	}
	.tb-hamburger:hover { color: var(--foreground); background: var(--hover-overlay); }

	/* Title fills the space between the ☰ (left) and + (right) — both 40px — so
	   it sits visually centered, and ellipsizes for long chat names. */
	.tb-context-title {
		flex: 1 1 auto;
		min-width: 0;
		text-align: center;
		padding: 0 4px;
		font-family: var(--font-display, Georgia, serif);
		font-size: 1rem;
		font-weight: 700;
		letter-spacing: -0.01em;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.tb-new-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		/* Slightly smaller than the 40px ☰ hamburger: the filled primary
		   background reads heavier than the borderless menu icon, so 36px gives
		   the two buttons matching visual weight. */
		width: 36px;
		height: 36px;
		flex-shrink: 0;
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		cursor: pointer;
		transition: filter 120ms ease;
		-webkit-tap-highlight-color: transparent;
	}
	.tb-new-icon:hover { filter: brightness(1.08); }

	/* ===================================================================
	   Navigation drawer (Option A)
	   =================================================================== */
	.drawer-overlay {
		position: fixed;
		inset: 0;
		background: color-mix(in oklch, var(--background) 55%, transparent);
		backdrop-filter: blur(2px);
		-webkit-backdrop-filter: blur(2px);
		z-index: 90;
		animation: drawer-fade 160ms ease;
	}

	.drawer {
		position: fixed;
		top: 0;
		left: 0;
		bottom: 0;
		width: min(82vw, 320px);
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 10px;
		background: var(--card, var(--background));
		border-right: 1px solid var(--border);
		box-shadow: var(--shadow-l);
		z-index: 100;
		overflow-y: auto;
		animation: drawer-slide 200ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	.drawer-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 4px 4px 8px;
	}

	.drawer-close {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 34px;
		height: 34px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.drawer-close:hover { color: var(--foreground); background: var(--hover-overlay); }

	.drawer-search {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		height: 38px;
		padding: 0 12px;
		margin-bottom: 4px;
		background: var(--input-bg);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		color: var(--muted-foreground);
		font-size: 0.85rem;
		cursor: pointer;
		transition: border-color 120ms ease, color 120ms ease;
	}
	.drawer-search:hover { color: var(--foreground); border-color: var(--input-border-hover); }
	.drawer-search span { flex: 1 1 auto; text-align: left; }

	.drawer-nav {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.drawer-item {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		padding: 11px 12px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		color: var(--foreground);
		font-size: 0.9rem;
		font-weight: 500;
		text-align: left;
		cursor: pointer;
		transition: background-color 120ms ease, color 120ms ease;
		-webkit-tap-highlight-color: transparent;
	}
	.drawer-item:hover { background: var(--hover-overlay); }
	.drawer-item :global(svg) { color: var(--muted-foreground); flex-shrink: 0; }
	.drawer-item.active {
		color: var(--selection-text, var(--primary));
		background: var(--selection-bg-emphasis, color-mix(in oklch, var(--primary) 14%, transparent));
	}
	.drawer-item.active :global(svg) { color: var(--selection-text, var(--primary)); }

	.drawer-divider {
		height: 1px;
		margin: 6px 8px;
		background: var(--border);
	}

	.drawer-foot {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-top: auto;
		padding: 10px 8px 4px;
		border-top: 1px solid var(--border);
	}
	.drawer-avatar {
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--muted-foreground);
		flex-shrink: 0;
	}
	.drawer-user { display: flex; flex-direction: column; min-width: 0; }
	.drawer-user-name {
		font-size: 0.85rem;
		font-weight: 600;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.drawer-user-sub {
		font-size: 0.72rem;
		color: var(--muted-foreground);
	}

	@keyframes drawer-fade {
		from { opacity: 0; }
		to { opacity: 1; }
	}
	@keyframes drawer-slide {
		from { transform: translateX(-100%); }
		to { transform: translateX(0); }
	}

	/* Desktop responsive (only relevant when the desktop bar is shown, i.e.
	   isMobile is false but the window is still on the narrower side of the
	   desktop range). */
	@media (max-width: 860px) {
		.tb-search-label { display: none; }
		.tb-kbd { display: none; }
		.tb-tab span { display: none; }
		.tb-tab { padding: 0 10px; }
	}

	@media (prefers-reduced-motion: reduce) {
		.tb-menu,
		.drawer,
		.drawer-overlay { animation: none; }
	}
</style>
