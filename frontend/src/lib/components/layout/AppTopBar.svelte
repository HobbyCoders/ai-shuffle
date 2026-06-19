<script lang="ts">
	/**
	 * AppTopBar - Global navigation bar
	 *
	 * Replaces the sidebar's "Pages" nav as the primary navigation. Pages are
	 * siblings reached from one row: Sessions · Files · Notes · Build · Automations.
	 * The session list (SidePanel) becomes contextual to the Sessions section.
	 *
	 * Styled entirely with the app's existing theme tokens (no bespoke palette).
	 */
	import {
		Files,
		StickyNote,
		Hammer,
		CalendarClock,
		Users,
		Bot,
		Server,
		Puzzle,
		Webhook,
		ChevronDown,
		Search,
		Plus,
		MoreHorizontal,
		Settings,
		BarChart3,
		BookOpen,
		PanelLeft,
		MessagesSquare
	} from 'lucide-svelte';
	import { activeView } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';

	interface Props {
		isAdmin?: boolean;
		isMobile?: boolean;
		onNewSession: () => void;
		onOpenSessions: () => void;
		onOpenFeature: (type: ViewType) => void;
		onOpenSpotlight?: () => void;
		onToggleSidebar?: () => void;
	}

	let {
		isAdmin = false,
		isMobile = false,
		onNewSession,
		onOpenSessions,
		onOpenFeature,
		onOpenSpotlight,
		onToggleSidebar
	}: Props = $props();

	// Build group members — the agent-configuration pages collapse into one tab.
	const BUILD_MEMBERS: ViewType[] = ['profile', 'subagent', 'mcp', 'plugins', 'hooks'];

	const buildItems: { type: ViewType; label: string; icon: typeof Users }[] = [
		{ type: 'profile', label: 'Profiles', icon: Users },
		{ type: 'subagent', label: 'Subagents', icon: Bot },
		{ type: 'mcp', label: 'MCP Servers', icon: Server },
		{ type: 'plugins', label: 'Plugins', icon: Puzzle },
		{ type: 'hooks', label: 'Hooks', icon: Webhook }
	];

	const overflowItems: { type: ViewType; label: string; icon: typeof Settings; adminOnly?: boolean }[] = [
		{ type: 'docs', label: 'Docs', icon: BookOpen },
		{ type: 'stats', label: 'Stats', icon: BarChart3 },
		{ type: 'settings', label: 'Settings', icon: Settings, adminOnly: true }
	];

	const filteredOverflow = $derived(overflowItems.filter((i) => !i.adminOnly || isAdmin));

	const currentType = $derived($activeView?.type ?? null);
	const sessionsActive = $derived(currentType === 'chat');
	const buildActive = $derived(currentType ? BUILD_MEMBERS.includes(currentType) : false);

	function isActive(t: ViewType): boolean {
		return currentType === t;
	}

	// Dropdown state
	let buildOpen = $state(false);
	let overflowOpen = $state(false);
	let buildDropdownEl = $state<HTMLDivElement | null>(null);
	let overflowDropdownEl = $state<HTMLDivElement | null>(null);

	function closeMenus() {
		buildOpen = false;
		overflowOpen = false;
	}

	// Close on outside click (robust: ignore clicks inside the open menu/trigger).
	function handleWindowClick(e: MouseEvent) {
		const t = e.target as Node;
		if (buildOpen && buildDropdownEl && !buildDropdownEl.contains(t)) buildOpen = false;
		if (overflowOpen && overflowDropdownEl && !overflowDropdownEl.contains(t)) overflowOpen = false;
	}

	function pick(type: ViewType) {
		closeMenus();
		onOpenFeature(type);
	}

	const buildLabel = $derived.by(() => {
		const member = buildItems.find((b) => b.type === currentType);
		return member ? member.label : 'Build';
	});
</script>

<svelte:window onclick={handleWindowClick} />

<header class="topbar">
	<!-- Left: brand (+ mobile sessions toggle) -->
	<div class="tb-left">
		{#if isMobile}
			<button class="tb-icon-btn" onclick={onToggleSidebar} title="Sessions" aria-label="Toggle sessions">
				<PanelLeft size={18} strokeWidth={1.6} />
			</button>
		{/if}
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
	</div>

	<!-- Center: primary page navigation -->
	<nav class="tb-nav" aria-label="Workspace">
		<button class="tb-tab" class:active={sessionsActive} onclick={onOpenSessions}>
			<MessagesSquare size={16} strokeWidth={1.6} /><span>Sessions</span>
		</button>
		<button class="tb-tab" class:active={isActive('file-browser')} onclick={() => onOpenFeature('file-browser')}>
			<Files size={16} strokeWidth={1.6} /><span>Files</span>
		</button>
		<button class="tb-tab" class:active={isActive('notes')} onclick={() => onOpenFeature('notes')}>
			<StickyNote size={16} strokeWidth={1.6} /><span>Notes</span>
		</button>

		<!-- Build group dropdown -->
		<div class="tb-dropdown" bind:this={buildDropdownEl}>
			<button
				class="tb-tab"
				class:active={buildActive}
				aria-haspopup="true"
				aria-expanded={buildOpen}
				onclick={() => { overflowOpen = false; buildOpen = !buildOpen; }}
			>
				<Hammer size={16} strokeWidth={1.6} /><span>{buildActive ? buildLabel : 'Build'}</span>
				<ChevronDown size={13} strokeWidth={1.75} class="tb-caret {buildOpen ? 'flipped' : ''}" />
			</button>
			{#if buildOpen}
				<div class="tb-menu" role="menu">
					<div class="tb-menu-label">Configure agents</div>
					{#each buildItems as item}
						<button class="tb-menu-item" class:active={isActive(item.type)} role="menuitem" onclick={() => pick(item.type)}>
							<item.icon size={15} strokeWidth={1.6} /><span>{item.label}</span>
						</button>
					{/each}
				</div>
			{/if}
		</div>

		{#if isAdmin}
			<button class="tb-tab" class:active={isActive('automations')} onclick={() => onOpenFeature('automations')}>
				<CalendarClock size={16} strokeWidth={1.6} /><span>Automations</span>
			</button>
		{/if}
	</nav>

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
				class="tb-icon-btn"
				aria-haspopup="true"
				aria-expanded={overflowOpen}
				title="More"
				onclick={() => { buildOpen = false; overflowOpen = !overflowOpen; }}
			>
				<MoreHorizontal size={18} strokeWidth={1.6} />
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

	/* Side sections flex equally so the nav stays centered */
	.tb-left,
	.tb-right {
		display: flex;
		align-items: center;
		gap: 6px;
		flex: 1 1 0;
		min-width: 0;
	}
	.tb-right { justify-content: flex-end; }

	/* Brand */
	.brand {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-shrink: 0;
		padding-right: 8px;
		margin-right: 2px;
	}
	.brand-mark { color: var(--primary); display: flex; }
	.brand-name {
		font-family: var(--font-display, Georgia, serif);
		font-size: 0.95rem;
		font-weight: 700;
		letter-spacing: -0.01em;
		color: var(--foreground);
	}

	/* Nav — centered; overflow visible so dropdowns aren't clipped */
	.tb-nav {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 2px;
		flex: 0 1 auto;
		overflow: visible;
	}

	.tb-tab {
		display: inline-flex;
		align-items: center;
		gap: 7px;
		height: 34px;
		padding: 0 12px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		color: var(--muted-foreground);
		font-size: 0.8125rem;
		font-weight: 500;
		white-space: nowrap;
		cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.tb-tab:hover { color: var(--foreground); background: var(--hover-overlay); }
	.tb-tab.active { color: var(--selection-text); background: var(--selection-bg-emphasis); }
	.tb-tab :global(svg) { flex-shrink: 0; }
	.tb-tab :global(.tb-caret) { transition: transform 180ms ease; opacity: 0.7; }
	.tb-tab :global(.tb-caret.flipped) { transform: rotate(180deg); }

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
	.tb-menu-label {
		font-size: 0.625rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--muted-foreground);
		padding: 6px 8px 4px;
	}
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

	/* Icon button */
	.tb-icon-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 34px;
		height: 34px;
		flex-shrink: 0;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.tb-icon-btn:hover { color: var(--foreground); background: var(--hover-overlay); }

	:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	/* Responsive */
	@media (max-width: 860px) {
		.tb-search-label { display: none; }
		.tb-kbd { display: none; }
		.tb-tab span { display: none; }
		.tb-tab { padding: 0 9px; }
	}

	@media (max-width: 639px) {
		.topbar { height: 48px; padding: 0 8px; gap: 4px; }
		.brand-name { display: none; }
		.tb-new-label { display: none; }
		.tb-new { padding: 0; width: 34px; justify-content: center; }
	}

	@media (prefers-reduced-motion: reduce) {
		.tb-menu { animation: none; }
		.tb-tab :global(.tb-caret) { transition: none; }
	}
</style>
