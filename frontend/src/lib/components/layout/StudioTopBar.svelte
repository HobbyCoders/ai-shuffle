<script lang="ts">
	/**
	 * StudioTopBar - desktop top bar for the main panel (Studio Noir shell).
	 *
	 * Sidebar toggle + breadcrumbs (`project › session title`) on the left; the
	 * caller's action snippet (git actions, chat utilities) and the account
	 * badge on the right. It replaces the old AppTopBar tab row: page navigation
	 * now lives in the sidebar's workspace nav.
	 *
	 * The notification bell used to sit here too. It now lives only in the
	 * sidebar header, so there is exactly one bell on screen at any width
	 * instead of one per header that happens to be mounted.
	 *
	 * The bar renders inside MainPanel so the git action buttons it hosts are the
	 * existing GitHeaderActions instance with its existing handlers — nothing
	 * about those actions is reimplemented here.
	 */
	import type { Snippet } from 'svelte';
	import { PanelLeft, ChevronRight } from 'lucide-svelte';
	import { username } from '$lib/stores/auth.svelte';
	import SessionTitleMenu from './SessionTitleMenu.svelte';

	interface Props {
		/** Left crumb — the project (or section) the current view belongs to. */
		projectLabel?: string | null;
		/** Right crumb — the open session / page title. */
		title?: string | null;
		/** Bound chat session id. Null on New Chat and non-chat pages (inert title). */
		sessionId?: string | null;
		sidebarCollapsed?: boolean;
		onToggleSidebar?: () => void;
		/** Project crumb activation (returns to the sessions board). */
		onOpenProject?: () => void;
		/** Right cluster contents supplied by MainPanel. */
		actions?: Snippet;
	}

	let {
		projectLabel = null,
		title = null,
		sessionId = null,
		sidebarCollapsed = false,
		onToggleSidebar,
		onOpenProject,
		actions
	}: Props = $props();

	const initials = $derived.by(() => {
		const name = ($username ?? '').trim();
		if (!name) return '·';
		const parts = name.split(/[\s._-]+/).filter(Boolean);
		const letters = parts.length > 1 ? parts[0][0] + parts[1][0] : name.slice(0, 2);
		return letters.toUpperCase();
	});
</script>

<header class="topbar">
	{#if onToggleSidebar}
		<button
			class="tb-icon"
			onclick={onToggleSidebar}
			aria-label={sidebarCollapsed ? 'Show sidebar' : 'Hide sidebar'}
			title={sidebarCollapsed ? 'Show sidebar' : 'Hide sidebar'}
			aria-pressed={!sidebarCollapsed}
		>
			<PanelLeft size={17} strokeWidth={1.85} />
		</button>
	{/if}

	<nav class="crumbs" aria-label="Breadcrumb">
		{#if projectLabel}
			{#if onOpenProject}
				<button class="crumb-proj" onclick={onOpenProject}>{projectLabel}</button>
			{:else}
				<span class="crumb-proj">{projectLabel}</span>
			{/if}
			{#if title}
				<ChevronRight class="crumb-sep" size={14} strokeWidth={2} />
			{/if}
		{/if}
		{#if title}
			<SessionTitleMenu {title} sessionId={sessionId} />
		{/if}
	</nav>

	<div class="tb-right">
		{#if actions}
			{@render actions()}
		{/if}
		<div class="tb-sep" aria-hidden="true"></div>
		<span class="tb-avatar" title={$username || 'Account'}>{initials}</span>
	</div>
</header>

<style>
	.topbar {
		position: relative;
		z-index: 40;
		flex: none;
		height: 52px;
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 0 14px 0 10px;
		border-bottom: 1px solid var(--border);
		background: var(--background);
	}

	.tb-icon {
		width: 30px;
		height: 30px;
		flex: none;
		border: none;
		background: transparent;
		border-radius: 8px;
		display: grid;
		place-items: center;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background-color 0.13s ease, color 0.13s ease;
	}
	.tb-icon:hover { background: var(--accent); color: var(--foreground); }

	.crumbs {
		display: flex;
		align-items: center;
		gap: 7px;
		min-width: 0;
		flex: 0 1 auto;
	}
	.crumb-proj {
		border: none;
		background: transparent;
		padding: 0;
		font-size: 13px;
		font-family: inherit;
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		max-width: 220px;
		cursor: pointer;
		transition: color 0.13s ease;
	}
	.crumb-proj:hover { color: var(--foreground); }
	.crumbs :global(.crumb-sep) { color: var(--subtle-foreground); flex: none; }

	.tb-right {
		margin-left: auto;
		display: flex;
		align-items: center;
		gap: 8px;
		flex: none;
	}

	.tb-sep { width: 1px; height: 20px; background: var(--border); }

	.tb-avatar {
		width: 26px;
		height: 26px;
		border-radius: 50%;
		display: grid;
		place-items: center;
		font-size: 10.5px;
		font-weight: 600;
		background: linear-gradient(155deg, oklch(0.42 0.05 200), oklch(0.28 0.03 250));
		color: var(--foreground);
		box-shadow: 0 0 0 1px var(--border);
		flex: none;
	}

	:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	@media (prefers-reduced-motion: reduce) {
		.tb-icon, .crumb-proj { transition: none; }
	}
</style>
