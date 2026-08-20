<script lang="ts">
	/**
	 * AppTopBar - compact mobile app bar + navigation drawer.
	 *
	 * Rendered only at ≤1080px (phones AND unfolded foldables); AppLayout owns
	 * that width decision. The bar carries ☰ menu + current-page title +
	 * notifications + new session, and every navigation destination lives in the
	 * slide-in drawer, which hosts the same Sidebar component the desktop shell
	 * shows beside the main panel.
	 *
	 * On desktop the sidebar itself replaces this bar's old tab row, and the main
	 * panel's StudioTopBar carries the breadcrumbs and actions.
	 */
	import { Plus, Menu, X } from 'lucide-svelte';
	import { activeView } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import NotificationCenter from '$lib/components/NotificationCenter.svelte';
	import Sidebar from './Sidebar.svelte';

	interface Props {
		isAdmin?: boolean;
		onNewSession: () => void;
		onOpenSession: (sessionId: string) => void;
		onOpenFeature: (type: ViewType) => void;
		onOpenSpotlight?: () => void;
	}

	let {
		isAdmin = false,
		onNewSession,
		onOpenSession,
		onOpenFeature,
		onOpenSpotlight
	}: Props = $props();

	const BUILD_MEMBERS: ViewType[] = ['build', 'profile', 'mcp', 'plugins', 'hooks'];
	const currentType = $derived($activeView?.type ?? null);
	const buildActive = $derived(currentType ? BUILD_MEMBERS.includes(currentType) : false);

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
		return (currentType && TITLES[currentType]) || 'AI Shuffle';
	});

	// Navigation drawer state
	let drawerOpen = $state(false);

	function handleWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') drawerOpen = false;
	}

	// Run a drawer action and dismiss the drawer in one step.
	function drawerGo<T extends unknown[]>(action: (...args: T) => void) {
		return (...args: T) => {
			drawerOpen = false;
			action(...args);
		};
	}
</script>

<svelte:window onkeydown={handleWindowKeydown} />

<header class="topbar topbar-mobile">
	<button class="tb-hamburger" onclick={() => (drawerOpen = true)} aria-label="Open menu" aria-expanded={drawerOpen}>
		<Menu size={20} strokeWidth={1.8} />
	</button>

	<span class="tb-context-title">{mobileTitle}</span>

	<NotificationCenter />

	<button class="tb-new-icon" onclick={onNewSession} aria-label="New session (Cmd/Ctrl+N)" title="New session">
		<Plus size={18} strokeWidth={2.2} />
	</button>
</header>

<!-- Slide-in navigation drawer: hosts the full sidebar -->
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
		<Sidebar
			{isAdmin}
			onNewSession={drawerGo(onNewSession)}
			onOpenSession={drawerGo(onOpenSession)}
			onOpenFeature={drawerGo(onOpenFeature)}
			onOpenSpotlight={onOpenSpotlight ? drawerGo(onOpenSpotlight) : undefined}
		>
			{#snippet headerAction()}
				<button class="drawer-close" onclick={() => (drawerOpen = false)} aria-label="Close menu">
					<X size={18} strokeWidth={1.8} />
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
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 40px;
		height: 40px;
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

	/* Title fills the space between the ☰ (left) and + (right) — both 40px — so
	   it sits visually centered, and ellipsizes for long chat names. */
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
		border-radius: 8px;
		cursor: pointer;
		transition: filter 120ms ease;
		-webkit-tap-highlight-color: transparent;
	}
	.tb-new-icon:hover { filter: brightness(1.08); }

	/* Drawer */
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
		width: min(86vw, 300px);
		display: flex;
		flex-direction: column;
		background: var(--sidebar);
		border-right: 1px solid var(--sidebar-border);
		box-shadow: var(--shadow-l);
		z-index: 100;
		overflow: hidden;
		animation: drawer-slide 200ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	.drawer-close {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 30px;
		height: 30px;
		background: transparent;
		border: none;
		border-radius: 8px;
		color: var(--sidebar-muted);
		cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.drawer-close:hover { color: var(--foreground); background: var(--sidebar-accent); }

	@keyframes drawer-fade {
		from { opacity: 0; }
		to { opacity: 1; }
	}
	@keyframes drawer-slide {
		from { transform: translateX(-100%); }
		to { transform: translateX(0); }
	}

	:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	@media (prefers-reduced-motion: reduce) {
		.drawer,
		.drawer-overlay { animation: none; }
	}
</style>
