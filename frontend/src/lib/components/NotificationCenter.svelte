<script lang="ts">
	/**
	 * NotificationCenter — the bell + history panel for the unified notification
	 * system. Lives in the AppTopBar right cluster (always visible, independent of
	 * the chat view) and reads the persisted `history` slice of the notification
	 * store.
	 *
	 * - Bell button with an unread-count badge.
	 * - Dropdown panel listing persisted notifications (newest first) with
	 *   per-item mark-read + dismiss, plus mark-all-read and clear-all.
	 * - Uses the shared `header-dropdowns` store so opening this closes the
	 *   profile / branch / claude-status popovers (single-open-at-a-time).
	 * - Desktop: the panel is right-anchored under the button (the bell sits in the
	 *   top-bar right cluster) so it expands leftward and can't clip off-screen.
	 * - Outside-click + Escape to close. On mobile (≤1080px — the foldable-aware
	 *   header breakpoint) the panel is `position: fixed` so the right-edge anchor
	 *   can't clip it, mirroring ClaudeStatusIndicator.
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
	const unread = $derived($unreadCount);
	const showPanel = $derived($openHeaderDropdown === 'notifications');

	let rootRef = $state<HTMLDivElement | null>(null);

	const ICONS: Record<NotificationSeverity, typeof Info> = {
		error: AlertOctagon,
		warning: AlertTriangle,
		success: CheckCircle2,
		info: Info
	};

	function togglePanel() {
		const nowOpen = toggleDropdown('notifications');
		// Opening the bell marks everything seen — the badge is "new since last look".
		if (nowOpen && unread > 0) notify.markAllRead();
	}

	function handleClickOutside(e: MouseEvent) {
		if (!showPanel) return;
		if (rootRef && !rootRef.contains(e.target as Node)) closeDropdown('notifications');
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && showPanel) closeDropdown('notifications');
	}

	onDestroy(() => closeDropdown('notifications'));

	function onItemClick(n: AppNotification) {
		if (!n.read) notify.markRead(n.id);
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

<svelte:window onclick={handleClickOutside} onkeydown={handleKeydown} />

<div class="notif-center" bind:this={rootRef}>
	<button
		class="notif-btn"
		class:open={showPanel}
		class:has-unread={unread > 0}
		onclick={togglePanel}
		aria-haspopup="dialog"
		aria-expanded={showPanel}
		aria-label={unread > 0 ? `Notifications (${unread} unread)` : 'Notifications'}
		title="Notifications"
	>
		<Bell size={18} strokeWidth={1.5} />
		{#if unread > 0}
			<span class="notif-badge" aria-hidden="true">{unread > 99 ? '99+' : unread}</span>
		{/if}
	</button>

	{#if showPanel}
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
								<span class="item-icon" aria-hidden="true"><Icon size={15} strokeWidth={1.75} /></span>
								<span class="item-body">
									{#if n.title}<span class="item-title">{n.title}</span>{/if}
									<span class="item-message">{n.message}</span>
									<span class="item-time">{relativeTime(n.created_at)}</span>
								</span>
							</button>
							{#if n.action}
								<button
									class="item-action"
									onclick={(e) => {
										e.stopPropagation();
										n.action?.onClick();
									}}>{n.action.label}</button
								>
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

<style>
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
		min-width: 15px;
		height: 15px;
		padding: 0 3px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		font-size: 0.625rem;
		font-weight: 700;
		line-height: 1;
		color: var(--destructive-foreground, #fff);
		background: var(--destructive);
		border: 1.5px solid var(--card, var(--background));
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

	.head-action {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 4px 7px;
		font-size: 0.6875rem;
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
		padding: 10px 12px;
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
		font-weight: 600;
		color: var(--foreground);
		line-height: 1.3;
	}

	.item-message {
		font-size: 0.78125rem;
		color: var(--foreground);
		line-height: 1.4;
		word-break: break-word;
	}

	.item-time {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		margin-top: 1px;
	}

	.item-action {
		flex-shrink: 0;
		align-self: center;
		padding: 4px 9px;
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		border: none;
		border-radius: 6px;
		cursor: pointer;
		white-space: nowrap;
		transition: background-color 120ms ease;
	}

	.item-action:hover {
		background: color-mix(in oklch, var(--primary) 20%, transparent);
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

	/* Mobile / foldable (≤1080px, coarse pointer): the header reflows and an
	   absolutely-positioned panel anchored to this button would clip. Pin it as a
	   fixed near-full-width panel below the header — mirrors ClaudeStatusIndicator. */
	@media (max-width: 1080px) and (pointer: coarse) {
		.notif-btn {
			width: 36px;
			height: 36px;
		}

		.notif-panel {
			position: fixed;
			top: 58px;
			left: 8px;
			right: 8px;
			width: auto;
			max-width: none;
			max-height: calc(100vh - 120px);
			z-index: 200;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.notif-btn {
			transition: none;
		}
		.notif-panel {
			animation: none;
		}
	}
</style>
