<script lang="ts">
	/**
	 * NotificationCenter — the bell + history panel for the unified notification
	 * system. Lives in the desktop rail or phone drawer header and
	 * reads the persisted `history` slice of the notification
	 * store.
	 *
	 * - Bell button with an unread-count badge.
	 * - Dropdown panel listing persisted notifications (newest first) with
	 *   per-item mark-read + dismiss, plus mark-all-read and clear-all.
	 * - Uses the shared `header-dropdowns` store so opening this closes the
	 *   profile, branch, and git-action popovers (single-open-at-a-time).
	 * - Rail: a 300px panel floats just right of the rail, bottom-aligned with
	 *   the bell, over the list column and canvas. Header instances stay
	 *   right-anchored under their button.
	 * - Outside-click + Escape to close. On the mobile shell the panel is
	 *   `position: fixed` so the right-edge anchor can't clip it.
	 */
	import { onDestroy } from 'svelte';
	import {
		Bell,
		AlertOctagon,
		AlertTriangle,
		CheckCircle2,
		Info,
		Check,
		Trash2,
		X
	} from 'lucide-svelte';
	import {
		notify,
		notificationHistory,
		unreadCount,
		type AppNotification,
		type NotificationSeverity
	} from '$lib/stores/notifications';
	import { openHeaderDropdown, toggleDropdown, closeDropdown } from '$lib/stores/header-dropdowns';

	const history = $derived($notificationHistory);
	interface Props {
		/** The phone top bar's copy: shows only while something is unread (or its panel is open). */
		bar?: boolean;
		/** Desktop rail: 18px icon and a fixed-position history panel over the list column. */
		rail?: boolean;
	}
	let { bar = false, rail = false }: Props = $props();

	const unread = $derived($unreadCount);
	const dropdownId = $derived(bar ? 'notifications-bar' : 'notifications');
	const showPanel = $derived($openHeaderDropdown === dropdownId);

	let rootRef = $state<HTMLDivElement | null>(null);
	let bellRef = $state<HTMLButtonElement | null>(null);

	const ICONS: Record<NotificationSeverity, typeof Info> = {
		error: AlertOctagon,
		warning: AlertTriangle,
		success: CheckCircle2,
		info: Info
	};

	// Severity is otherwise carried only by icon shape and colour.
	const SEVERITY_LABELS: Record<NotificationSeverity, string> = {
		error: 'Error',
		warning: 'Warning',
		success: 'Success',
		info: 'Info'
	};

	// The rail's panel floats beside the rail, its bottom level with the bell,
	// at 300px: from x=64 its right edge lands at 364, inside the list column
	// and its gutter, 12px or more left of the page content (x=378 with the
	// list panel open). It never covers the rail and keeps inside the window.
	const RAIL_PANEL_WIDTH = 300;
	// The page's blank inline gutter right of the list column: overrunning the
	// list panel by up to this much covers only padding, never text.
	const CONTENT_GUTTER = 48;
	let railAnchor = $state({ left: 0, bottom: 8, width: RAIL_PANEL_WIDTH, railRight: 0 });
	// With the list panel collapsed (or a narrow window) the panel has to sit on
	// page content, so a light scrim dims the page and no sliced words read.
	let coversPage = $state(false);
	function positionPanel() {
		if (!rail || !bellRef) return;
		const rect = bellRef.getBoundingClientRect();
		const railRight = bellRef.closest('nav.rail')?.getBoundingClientRect().right ?? rect.right;
		const width = Math.min(RAIL_PANEL_WIDTH, window.innerWidth - 16);
		const left = Math.max(8, Math.min(railRight + 8, window.innerWidth - width - 8));
		railAnchor = { left, bottom: Math.max(8, window.innerHeight - rect.bottom), width, railRight };
		const listRight = bellRef.closest('aside')?.getBoundingClientRect().right ?? railRight;
		coversPage = left + width > listRight + CONTENT_GUTTER;
	}

	function togglePanel() {
		positionPanel();
		const nowOpen = toggleDropdown(dropdownId);
		// Opening the bell marks everything seen — the badge is "new since last look".
		if (nowOpen && unread > 0) notify.markAllRead();
	}

	function handleClickOutside(e: MouseEvent) {
		if (!showPanel) return;
		if (rootRef && !rootRef.contains(e.target as Node)) closeDropdown(dropdownId);
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key !== 'Escape' || !showPanel) return;
		e.preventDefault();
		// Hand focus back to the bell rather than dropping it on <body>.
		const focusWasInside = !!rootRef?.contains(document.activeElement);
		closeDropdown(dropdownId);
		if (focusWasInside) bellRef?.focus();
	}

	onDestroy(() => closeDropdown(dropdownId));

	function onItemClick(n: AppNotification) {
		if (!n.read) notify.markRead(n.id);
		// Session-scoped notifications (e.g. "Session finished") deep-link to their
		// session through the shared 'open-session' navigation event.
		if (n.session_id) {
			window.dispatchEvent(new CustomEvent('open-session', { detail: { sessionId: n.session_id } }));
			closeDropdown(dropdownId);
		}
	}

	function relativeTime(ms: number): string {
		if (!ms) return '';
		const diff = Date.now() - ms;
		if (diff < 0) return 'just now';
		const sec = Math.floor(diff / 1000);
		if (sec < 60) return 'just now';
		const min = Math.floor(sec / 60);
		if (min < 60) return `${min}m ago`;
		const hr = Math.floor(min / 60);
		if (hr < 24) return `${hr}h ago`;
		const day = Math.floor(hr / 24);
		if (day < 7) return `${day}d ago`;
		const wk = Math.floor(day / 7);
		return `${wk}w ago`;
	}
</script>

<svelte:window onclick={handleClickOutside} onkeydown={handleKeydown} onresize={positionPanel} />

{#if !bar || unread > 0 || showPanel}
<div class="notif-center" class:bar class:rail bind:this={rootRef} style:--notif-left={`${railAnchor.left}px`} style:--notif-bottom={`${railAnchor.bottom}px`} style:--notif-width={`${railAnchor.width}px`} style:--notif-rail-right={`${railAnchor.railRight}px`}>
	<button
		bind:this={bellRef}
		class="notif-btn"
		class:open={showPanel}
		class:has-unread={unread > 0}
		onclick={togglePanel}
		aria-haspopup="dialog"
		aria-expanded={showPanel}
		aria-label={unread > 0 ? `Notifications (${unread} unread)` : 'Notifications'}
		title="Notifications"
	>
		<Bell size={bar ? 16 : 18} strokeWidth={bar ? 1.8 : 1.75} />
		{#if unread > 0}
			<span class="notif-badge" aria-hidden="true">{unread > 99 ? '99+' : unread}</span>
		{/if}
	</button>

	{#if showPanel}
		{#if rail && coversPage}
			<!-- Dims the page the panel sits on; a click here closes like any outside click. -->
			<button type="button" class="notif-scrim" tabindex="-1" aria-label="Close notifications" data-testid="notif-scrim" onclick={() => closeDropdown(dropdownId)}></button>
		{/if}
		<div class="notif-panel" role="dialog" aria-label="Notifications">
			<div class="panel-head">
				<span class="panel-title">Notifications</span>
				<div class="panel-head-actions">
					{#if history.length > 0}
						{#if unread > 0}
							<button class="head-action" onclick={() => notify.markAllRead()} title="Mark all read">
								<Check size={13} strokeWidth={1.75} /> Mark read
							</button>
						{/if}
						<button class="head-action danger" onclick={() => notify.clearAll()} title="Clear all">
							<Trash2 size={13} strokeWidth={1.75} /> Clear
						</button>
					{/if}
				</div>
			</div>

			<div class="panel-list">
				{#if history.length === 0}
					<div class="panel-empty">
						<Bell size={22} strokeWidth={1.25} />
						<span>No notifications</span>
					</div>
				{:else}
					{#each history as n (n.id)}
						{@const Icon = ICONS[n.severity]}
						<div class="notif-item sev-{n.severity}" class:unread={!n.read}>
							<button type="button" class="item-main" onclick={() => onItemClick(n)}>
								<span class="item-icon" role="img" aria-label={SEVERITY_LABELS[n.severity]}><Icon size={15} strokeWidth={1.75} aria-hidden="true" /></span>
								<span class="item-body">
									{#if n.title}<span class="item-title" title={n.title}>{n.title}</span>{/if}
									<span class="item-message" title={n.message}>{n.message}</span>
									<span class="item-time">{relativeTime(n.created_at)}</span>
								</span>
							</button>
							{#if n.action || n.secondaryAction}
								<div class="item-actions">
									{#each [n.action, n.secondaryAction] as act, i (i)}
										{#if act}
											<button
												class="item-action"
												class:secondary={i === 1}
												onclick={(e) => {
													e.stopPropagation();
													act.onClick();
												}}>{act.label}</button
											>
										{/if}
									{/each}
								</div>
							{/if}
							<button
								class="item-dismiss"
								onclick={(e) => {
									e.stopPropagation();
									notify.dismiss(n.id);
								}}
								aria-label="Dismiss notification"
								title="Dismiss"
							>
								<X size={13} strokeWidth={2} />
							</button>
						</div>
					{/each}
				{/if}
			</div>
		</div>
	{/if}
</div>
{/if}

<style>
	/* Rail controls share its 36px rhythm; history opens over the list column. */
	.notif-center.rail .notif-btn { width: 36px; height: 36px; border-radius: var(--radius-control); }
	.notif-center.rail .notif-btn:hover,
	.notif-center.rail .notif-btn.open { background: var(--row-hover); }
	.notif-center.rail .notif-btn:active { background: var(--row-hover); transform: scale(.95); }
	.notif-center.rail .notif-badge { top: 0; right: -1px; box-shadow: 0 0 0 1.5px var(--sidebar); }
	.notif-center.rail .notif-panel {
		position: fixed;
		top: auto;
		right: auto;
		left: var(--notif-left);
		bottom: var(--notif-bottom);
		width: var(--notif-width);
		max-height: min(440px, calc(100dvh - var(--notif-bottom) - var(--titlebar-height, 0px) - 8px));
	}
	/* Half the shared modal overlay, from the rail's edge: the rail stays lit. */
	.notif-scrim {
		position: fixed;
		inset: var(--titlebar-height, 0px) 0 0 var(--notif-rail-right);
		z-index: 99;
		padding: 0;
		border: 0;
		cursor: default;
		background: color-mix(in oklch, var(--modal-overlay) 50%, transparent);
		animation: notif-scrim-in 120ms ease-out;
	}
	@keyframes notif-scrim-in { from { opacity: 0; } }

	/* Phone top bar: a 44x48 target, a 16px bell and the one 16px count. */
	.notif-center.bar .notif-btn { width: 44px; height: 48px; border-radius: 8px; opacity: 1; }
	.notif-center.bar .notif-badge {
		top: 7px;
		right: auto;
		left: 23px;
		border: 0;
		box-shadow: 0 0 0 1.5px var(--background);
	}
	.notif-center.bar .notif-panel {
		position: fixed;
		top: 58px;
		left: 8px;
		right: 8px;
		width: auto;
		max-height: calc(100dvh - 120px);
	}

	.notif-center {
		position: relative;
		display: flex;
		align-items: center;
	}

	.notif-btn {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		/* Match the 34px top-bar buttons (account / search / new session). */
		width: 34px;
		height: 34px;
		flex-shrink: 0;
		background: transparent;
		border: none;
		border-radius: 50%;
		color: var(--muted-foreground);
		cursor: pointer;
		opacity: 0.65;
		transition: background-color 150ms ease, color 150ms ease, opacity 150ms ease;
		-webkit-tap-highlight-color: transparent;
	}

	.notif-btn:hover,
	.notif-btn.open,
	.notif-btn.has-unread {
		opacity: 1;
	}

	/* In the sidebar header it matches the filter button beside it. */
	.notif-center:not(.bar) .notif-btn {
		color: var(--sidebar-muted);
		opacity: 1;
	}
	.notif-center:not(.bar) .notif-btn:hover,
	.notif-center:not(.bar) .notif-btn.open {
		color: var(--foreground);
	}

	.notif-btn:hover,
	.notif-btn.open {
		background: var(--hover-overlay);
	}

	.notif-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	.notif-badge {
		position: absolute;
		top: -2px;
		right: -2px;
		box-sizing: content-box;
		min-width: 10px;
		height: 16px;
		padding: 0 3px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		font-size: 11px;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		line-height: 1;
		/* Unread is news, not an error: --destructive stays for real failures. */
		color: var(--primary-foreground);
		background: var(--primary);
		border: 0;
		box-shadow: 0 0 0 1.5px var(--card, var(--background));
		border-radius: 999px;
		pointer-events: none;
	}

	/* Desktop: right-anchored under the button (top-bar right cluster), expands left. */
	.notif-panel {
		position: absolute;
		top: calc(100% + 6px);
		right: 0;
		z-index: 100;
		width: min(360px, calc(100vw - 24px));
		max-height: min(440px, calc(100vh - 120px));
		display: flex;
		flex-direction: column;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.83);
		box-shadow: var(--shadow-l);
		overflow: hidden;
		animation: notif-in 120ms cubic-bezier(0.16, 1, 0.3, 1);
		container-type: inline-size;
	}

	@keyframes notif-in {
		from { opacity: 0; transform: translateY(-4px); }
		to { opacity: 1; transform: translateY(0); }
	}

	.panel-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		padding: 10px 12px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.panel-title {
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--foreground);
	}

	.panel-head-actions {
		display: flex;
		align-items: center;
		gap: 4px;
	}

	/* Ghost buttons, 28px tall with 13px text. */
	.head-action {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		height: 28px;
		padding: 0 8px;
		font-size: 13px;
		font-weight: 500;
		color: var(--muted-foreground);
		background: transparent;
		border: none;
		border-radius: 6px;
		cursor: pointer;
		transition: background-color 120ms ease, color 120ms ease;
	}

	.head-action:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.head-action.danger:hover {
		color: var(--destructive);
	}

	.panel-list {
		overflow-y: auto;
		overscroll-behavior: contain;
	}

	.panel-empty {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 8px;
		padding: 32px 16px;
		color: var(--muted-foreground);
		font-size: 0.8125rem;
		opacity: 0.7;
	}

	.notif-item {
		position: relative;
		display: flex;
		align-items: flex-start;
		gap: 9px;
		padding: 12px;
		border-bottom: 1px solid var(--border);
		transition: background-color 120ms ease;
	}

	/* The row's main click target (mark-read / open) — a real button so the
	   action/dismiss buttons aren't nested inside an interactive element. */
	.item-main {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: flex-start;
		gap: 9px;
		padding: 0;
		background: none;
		border: none;
		font: inherit;
		color: inherit;
		text-align: left;
		cursor: pointer;
	}

	.item-main:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
		border-radius: 6px;
	}

	.notif-item:last-child {
		border-bottom: none;
	}

	.notif-item:hover {
		background: var(--hover-overlay);
	}

	.notif-item.unread {
		background: color-mix(in oklch, var(--primary) 5%, transparent);
	}

	.notif-item.unread::before {
		content: '';
		position: absolute;
		left: 4px;
		top: 50%;
		transform: translateY(-50%);
		width: 3px;
		height: 18px;
		border-radius: 2px;
		background: var(--primary);
	}

	.item-icon {
		flex-shrink: 0;
		display: inline-flex;
		margin-top: 1px;
	}

	.sev-error .item-icon { color: var(--destructive); }
	.sev-warning .item-icon { color: var(--warning, #d97706); }
	.sev-success .item-icon { color: var(--success, #059669); }
	.sev-info .item-icon { color: var(--muted-foreground); }

	.item-body {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.item-title {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
		line-height: 1.3;
	}

	.item-message {
		font-size: 0.8125rem;
		font-weight: 400;
		color: var(--muted-foreground);
		line-height: 1.5;
		word-break: break-word;
	}

	/* Desktop popover: a title on one line and a body on two; the full text
	   stays in the row's tooltip and for screen readers. The phone panel keeps full text. */
	/* Desktop popovers wear the one menu surface the model picker uses; the phone panel keeps its own. */
	.notif-center:not(.bar) .notif-panel {
		background: var(--popover);
		border-color: var(--menu-border);
		border-radius: var(--menu-radius);
		box-shadow: var(--menu-shadow);
	}
	.notif-center:not(.bar) .item-title {
		font-size: 13px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.notif-center:not(.bar) .item-message {
		font-size: 13px;
		display: -webkit-box;
		-webkit-box-orient: vertical;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		overflow: hidden;
	}

	.item-time {
		font-size: 12px;
		font-weight: 400;
		color: var(--muted-foreground);
		margin-top: 1px;
	}

	.item-actions {
		flex-shrink: 0;
		align-self: center;
		display: flex;
		flex-direction: column;
		align-items: stretch;
		gap: 4px;
		max-width: 9.5rem;
	}

	/* A narrow panel, and always the rail's 300px popover, moves the actions
	   onto their own line under the message, so the message keeps the row's width. */
	@container (max-width: 320px) {
		.notif-item { flex-wrap: wrap; }
		.item-main { order: 1; }
		.item-dismiss { order: 2; }
		.item-actions {
			order: 3;
			flex: 1 0 100%;
			flex-direction: row;
			flex-wrap: wrap;
			max-width: none;
			padding-left: 24px;
		}
	}
	.notif-center.rail .notif-item { flex-wrap: wrap; }
	.notif-center.rail .item-main { order: 1; }
	.notif-center.rail .item-dismiss { order: 2; }
	.notif-center.rail .item-actions {
		order: 3;
		flex: 1 0 100%;
		flex-direction: row;
		flex-wrap: wrap;
		max-width: none;
		padding-left: 24px;
	}

	.item-action {
		flex-shrink: 0;
		text-align: center;
		white-space: normal;
		padding: 4px 9px;
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		border: none;
		border-radius: 6px;
		cursor: pointer;
		transition: background-color 120ms ease;
	}

	.item-action:hover {
		background: color-mix(in oklch, var(--primary) 20%, transparent);
	}

	.item-action.secondary {
		color: var(--muted-foreground);
		background: transparent;
		box-shadow: inset 0 0 0 1px var(--border);
	}

	.item-action.secondary:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	.item-dismiss {
		flex-shrink: 0;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 22px;
		height: 22px;
		padding: 0;
		color: var(--muted-foreground);
		background: transparent;
		border: none;
		border-radius: 5px;
		cursor: pointer;
		opacity: 0.6;
		transition: opacity 120ms ease, background-color 120ms ease, color 120ms ease;
	}

	.notif-item:hover .item-dismiss {
		opacity: 0.9;
	}

	.item-dismiss:hover {
		opacity: 1;
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.head-action:focus-visible,
	.item-action:focus-visible,
	.item-dismiss:focus-visible,
	.notif-item:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	@media (prefers-reduced-motion: reduce) {
		.notif-btn {
			transition: none;
		}
		.notif-panel,
		.notif-scrim {
			animation: none;
		}
	}
</style>
