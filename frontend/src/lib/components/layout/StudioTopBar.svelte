<script lang="ts">
	/**
	 * StudioTopBar - desktop top bar for the main panel (Studio Noir shell).
	 *
	 * Sidebar toggle + breadcrumbs (`project › session title`) on the left; the
	 * caller's action snippet (git actions, chat utilities) on the right.
	 * It replaces the old AppTopBar tab row: page navigation
	 * now lives in the sidebar's workspace nav.
	 *
	 * The notification bell lives in the sidebar header. At phone width, where
	 * the sidebar is a drawer, the bar adds its own while something is unread.
	 *
	 * The bar renders inside MainPanel so the git action buttons it hosts are the
	 * existing GitHeaderActions instance with its existing handlers — nothing
	 * about those actions is reimplemented here.
	 */
	import type { Snippet } from 'svelte';
	import { ChevronLeft, Menu, PanelLeft } from 'lucide-svelte';
	import { phonePanelActions } from './view/ViewPanel.svelte';
	import { phoneBack } from '../settings/shell/BackRow.svelte';
	import SessionBreadcrumbs from './SessionBreadcrumbs.svelte';
	import NotificationCenter from '$lib/components/NotificationCenter.svelte';
	import { unreadCount } from '$lib/stores/notifications';
	import { NEW_CHAT_LABEL } from './chat-empty-state';
	import { workerInstalls } from '$lib/stores/worker-presence';
	import { selfWorkerName } from '$lib/components/ui/worker-name';

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
		/** The empty new chat: at phone width the bar titles it, like the phone app. */
		newChat?: boolean;
		/** Draw the rule under the bar. Off until a chat has a title of its own. */
		rule?: boolean;
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
		newChat = false,
		rule = true,
		actions
	}: Props = $props();

	// At phone width this button stops being a panel toggle and becomes the way
	// to the session list, drawn the way the phone app draws it.
	let phoneWidth = $state(false);
	$effect(() => {
		const query = window.matchMedia?.('(max-width: 600px)');
		if (!query) return;
		phoneWidth = query.matches;
		const change = (event: MediaQueryListEvent) => { phoneWidth = event.matches; };
		query.addEventListener?.('change', change);
		return () => query.removeEventListener?.('change', change);
	});
	const phoneHost = Symbol('responsive-bar');
	$effect(() => {
		if (!phoneWidth) return;
		phoneBack.host = phoneHost;
		return () => { if (phoneBack.host === phoneHost) phoneBack.host = null; };
	});
</script>

{#snippet workerLine()}
	<span class="tb-newchat-worker"><i aria-hidden="true"></i><b>{selfWorkerName($workerInstalls)}</b></span>
{/snippet}

<header class="topbar" class:no-rule={!rule}>
	{#if phoneWidth && phoneBack.onclick}
		<button class="tb-back" onclick={phoneBack.onclick} aria-label={`Back to ${phoneBack.label}`}><ChevronLeft size={20} /><span>{phoneBack.label}</span></button>
	{:else if onToggleSidebar}
		<button
			class="tb-icon"
			onclick={onToggleSidebar}
			class:phone={phoneWidth}
			aria-label={phoneWidth ? ($unreadCount > 0 ? `Chats (${$unreadCount} unread notifications)` : 'Chats') : sidebarCollapsed ? 'Show sidebar' : 'Hide sidebar'}
			title={phoneWidth ? undefined : sidebarCollapsed ? 'Show sidebar' : 'Hide sidebar'}
			aria-pressed={!sidebarCollapsed}
		>
			{#if phoneWidth}
				<Menu size={20} strokeWidth={1.8} />
			{:else}
				<PanelLeft size={17} strokeWidth={1.85} />
			{/if}
		</button>
	{/if}

	{#if phoneWidth && phoneBack.onclick}
		<!-- The parent name is already in the back control. -->
	{:else if phoneWidth && newChat}
		<!-- The phone app's bar (AppTopBar): the title over the worker this page talks to.
		     The worker is changed from Chat controls, under the composer. -->
		<div class="tb-newchat">
			<span class="tb-newchat-title">{NEW_CHAT_LABEL}</span>
			{@render workerLine()}
		</div>
	{:else if phoneWidth && sessionId}
		<!-- An existing chat keeps the worker line, the same as the phone app's bar. -->
		<div class="tb-newchat tb-chat">
			<SessionBreadcrumbs {projectLabel} {title} {sessionId} {onOpenProject} />
			{@render workerLine()}
		</div>
	{:else}
		<div class="tb-page-label" class:panel-open={!sessionId && phonePanelActions.visible} aria-label={title ?? projectLabel ?? undefined}><SessionBreadcrumbs {projectLabel} {title} {sessionId} {onOpenProject} /></div>
	{/if}

	<div class="tb-right">
		{#if phoneWidth && phoneBack.onclick}{@render phoneBack.status?.()}{:else if phoneWidth && phonePanelActions.label === title}{@render phonePanelActions.content?.()}{/if}
		{#if actions}
			{@render actions()}
		{/if}
		{#if phoneWidth && phoneBack.onclick && onToggleSidebar}<button class="tb-icon phone" aria-label="Chats" title="Navigation" onclick={onToggleSidebar}><Menu size={20} strokeWidth={1.8} /></button>{/if}
			{#if phoneWidth}<NotificationCenter bar />{/if}
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

	.topbar.no-rule { border-bottom-color: transparent; }

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
	.tb-back { display: flex; align-items: center; gap: 8px; min-height: 44px; padding: 0 12px; border: 0; border-radius: 8px; background: none; color: var(--foreground); font-size: 15px; }
	.tb-back:hover { background: var(--accent); }
	.tb-back:active { background: var(--row-selected); }
	.tb-page-label { display: flex; min-width: 0; }
	@media (min-width: 601px) and (max-width: 1080px) { .tb-page-label.panel-open { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); } }
	.tb-icon:hover { background: var(--accent); color: var(--foreground); }
	.tb-icon.phone { width: 44px; height: 48px; color: var(--foreground); }

	.tb-right {
		margin-left: auto;
		display: flex;
		align-items: center;
		gap: 8px;
		flex: none;
	}

	/* Same lines as AppTopBar: title from 8px down, worker 7px above the bar's edge. */
	.tb-newchat {
		flex: 1 1 auto;
		align-self: stretch;
		display: flex;
		flex-direction: column;
		min-width: 0;
		padding: 8px 2px 0 4px;
	}
	.tb-chat > :global(.crumbs) {
		flex: none;
		height: 21px;
		min-width: 0;
	}
	.tb-newchat-title {
		font-size: 15px;
		line-height: 21px;
		font-weight: 500;
		letter-spacing: -0.018em;
		color: var(--foreground);
	}
	.tb-newchat-worker {
		display: flex;
		align-items: center;
		gap: 5px;
		min-width: 0;
		margin-top: 4px;
		font-size: 11.5px;
		line-height: 16px;
		color: var(--muted-foreground);
	}
	.tb-newchat-worker i {
		flex: none;
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--success, var(--primary));
	}
	.tb-newchat-worker b {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 600;
	}

	/* The phone app's bar: same height, same edges, no gaps between its 44px targets. */
	@media (max-width: 600px) {
		.topbar { height: 56px; gap: 0; padding: 0 4px; }
		.tb-right { gap: 0; }
	}

	:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	@media (prefers-reduced-motion: reduce) {
		.tb-icon { transition: none; }
	}
</style>
