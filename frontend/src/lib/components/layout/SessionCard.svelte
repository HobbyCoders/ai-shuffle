<script lang="ts">
	/**
	 * SessionCard - three-line session row for the Studio Noir sidebar.
	 *
	 * L1: project · status · timer (row actions sit at the top-right)
	 * L2: session title
	 * L3: device · branch
	 *
	 * The mockup's third line also carries a +add/−del diffstat and a provider
	 * glyph; neither is derivable from the sessions store without new API work,
	 * so those two segments are omitted rather than faked.
	 *
	 * Hover/swipe exposes a single action: Settle (active) or Move to Active
	 * (settled). Rename, Fork, and Delete live on the header session-title menu.
	 */
	import { onDestroy, onMount } from 'svelte';
	import {
		Loader2,
		AlertCircle,
		Check,
		Monitor,
		Cloud,
		GitBranch,
		Undo2
	} from 'lucide-svelte';
	import type { Session } from '$lib/api/client';
	import { appData } from '$lib/stores/app-data.svelte';
	import { toast } from '$lib/stores/toast';
	import {
		STATUS_LABELS,
		isLiveStatus,
		formatElapsed,
		formatAge,
		deviceLabel,
		branchLabel,
		sessionTimestamp,
		type SessionCardStatus
	} from './session-card-data';

	interface Props {
		session: Session;
		status: SessionCardStatus;
		/** Display name of the session's project. */
		projectLabel: string;
		/** True when this session is the one open in the main panel. */
		active?: boolean;
		/** Current clock (ms) supplied by the sidebar's shared ticker. */
		now: number;
		/** Single-line dense row — used for the settled/archived groups. */
		compact?: boolean;
		onOpen: (sessionId: string) => void;
	}

	let { session, status, projectLabel, active = false, now, compact = false, onOpen }: Props = $props();

	const live = $derived(isLiveStatus(status));
	const elapsed = $derived(Math.max(0, now - sessionTimestamp(session)));
	const timer = $derived(live ? formatElapsed(elapsed) : formatAge(elapsed));
	const title = $derived(session.title?.trim() || 'Untitled');
	const device = $derived(deviceLabel(session));
	const branch = $derived(branchLabel(session));
	const statusLabel = $derived(STATUS_LABELS[status]);
	// In-flight (working) rows that aren't the open session recede so the list
	// triages at a glance; awaiting/done rows stay prominent because they need you.
	const receded = $derived(status === 'working' && !active);

	let cardRef = $state<HTMLElement | null>(null);
	let actionsOpen = $state(false);
	let swipeLayout = $state(false);
	let dragOffset = $state<number | null>(null);
	let suppressClickTimer: ReturnType<typeof setTimeout> | null = null;

	const ACTION_REVEAL_WIDTH = 44;
	const SWIPE_COMMIT_DISTANCE = 44;
	let pointerId: number | null = null;
	let pointerStartX = 0;
	let pointerStartY = 0;
	let pointerStartOffset = 0;
	let horizontalDrag = false;
	let suppressNextClick = false;

	function closeActions() {
		actionsOpen = false;
		dragOffset = null;
	}

	function handleWindowClick(event: MouseEvent) {
		if (actionsOpen && cardRef && !cardRef.contains(event.target as Node)) closeActions();
	}

	function handleWindowKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') closeActions();
	}

	async function toggleSettled(event: MouseEvent | KeyboardEvent) {
		event.stopPropagation();
		actionsOpen = false;
		try {
			await appData.setSessionActive(session.id, !session.is_active);
		} catch {
			toast.push('Failed to update session', { tone: 'error' });
		}
	}

	function openCard() {
		if (suppressNextClick) {
			suppressNextClick = false;
			return;
		}
		if (swipeLayout && actionsOpen) {
			closeActions();
			return;
		}
		onOpen(session.id);
	}

	function handleCardKeydown(event: KeyboardEvent) {
		if (event.target !== event.currentTarget) return;
		if (event.key === 'Enter' || event.key === ' ') {
			event.preventDefault();
			openCard();
		}
	}

	function isActionTarget(target: EventTarget | null): boolean {
		return target instanceof Element && !!target.closest('.card-actions, button, input, a');
	}

	function handlePointerDown(event: PointerEvent) {
		if (!swipeLayout || event.isPrimary === false || event.button !== 0 || isActionTarget(event.target)) return;
		pointerId = event.pointerId;
		pointerStartX = event.clientX;
		pointerStartY = event.clientY;
		pointerStartOffset = actionsOpen ? -ACTION_REVEAL_WIDTH : 0;
		horizontalDrag = false;
		cardRef?.setPointerCapture?.(event.pointerId);
	}

	function handlePointerMove(event: PointerEvent) {
		if (pointerId !== event.pointerId) return;
		const dx = event.clientX - pointerStartX;
		const dy = event.clientY - pointerStartY;

		if (!horizontalDrag) {
			if (Math.abs(dx) < 6 && Math.abs(dy) < 6) return;
			if (Math.abs(dy) >= Math.abs(dx)) {
				pointerId = null;
				dragOffset = null;
				return;
			}
			horizontalDrag = true;
		}

		event.preventDefault();
		dragOffset = Math.max(-ACTION_REVEAL_WIDTH, Math.min(0, pointerStartOffset + dx));
	}

	function finishPointer(event: PointerEvent) {
		if (pointerId !== event.pointerId) return;
		const dx = event.clientX - pointerStartX;
		if (horizontalDrag) {
			const nextOffset = dragOffset ?? pointerStartOffset;
			actionsOpen = actionsOpen
				? nextOffset < -(ACTION_REVEAL_WIDTH - SWIPE_COMMIT_DISTANCE)
				: nextOffset <= -SWIPE_COMMIT_DISTANCE;
			suppressNextClick = true;
			if (suppressClickTimer !== null) clearTimeout(suppressClickTimer);
			suppressClickTimer = setTimeout(() => {
				suppressNextClick = false;
				suppressClickTimer = null;
			}, 0);
		} else if (Math.abs(dx) > 6) {
			suppressNextClick = true;
		}
		dragOffset = null;
		pointerId = null;
		horizontalDrag = false;
	}

	function cancelPointer(event: PointerEvent) {
		if (pointerId !== event.pointerId) return;
		dragOffset = null;
		pointerId = null;
		horizontalDrag = false;
	}

	onMount(() => {
		if (typeof window.matchMedia !== 'function') {
			const syncWidth = () => {
				swipeLayout = window.innerWidth <= 1080;
				if (!swipeLayout) closeActions();
			};
			syncWidth();
			window.addEventListener('resize', syncWidth);
			return () => window.removeEventListener('resize', syncWidth);
		}

		const query = window.matchMedia('(hover: none), (max-width: 1080px)');
		const syncLayout = () => {
			swipeLayout = query.matches;
			if (!swipeLayout) closeActions();
		};
		syncLayout();
		query.addEventListener('change', syncLayout);
		return () => query.removeEventListener('change', syncLayout);
	});

	onDestroy(() => {
		if (suppressClickTimer !== null) clearTimeout(suppressClickTimer);
	});
</script>

<svelte:window onclick={handleWindowClick} onkeydown={handleWindowKeydown} />

<div
	bind:this={cardRef}
	class="scard-frame"
	class:compact
	class:actions-open={actionsOpen}
	class:dragging={dragOffset !== null}
	role="group"
	aria-label={`${title} session`}
	onpointerdown={handlePointerDown}
	onpointermove={handlePointerMove}
	onpointerup={finishPointer}
	onpointercancel={cancelPointer}
>
	<div
		class="scard"
		class:active
		class:receded
		class:compact
		role="button"
		tabindex="0"
		aria-current={active ? 'true' : undefined}
		onclick={openCard}
		onkeydown={handleCardKeydown}
		title={title}
		style:transform={dragOffset === null ? undefined : `translateX(${dragOffset}px)`}
	>
		{#if compact}
			<span class="l1 cline">
				<span class="ctitle">{title}</span>
				<span class="timer" class:live>{timer}</span>
			</span>
			<span class="l3">
				<span class="seg">
					<span class="cproj">{projectLabel}</span>
				</span>
				<span class="seg">
					{#if session.is_remote}
						<Cloud size={12} strokeWidth={1.9} />
					{:else}
						<Monitor size={12} strokeWidth={1.9} />
					{/if}
					<span class="dev">{device}</span>
				</span>
				{#if branch}
					<span class="seg">
						<GitBranch size={12} strokeWidth={1.9} />
						<span class="branch">{branch}</span>
					</span>
				{/if}
			</span>
		{:else}
			<span class="l1">
				<span class="proj">{projectLabel}</span>
				<span class="dot proj-sep" aria-hidden="true"></span>
				<span class="status {status}">
					{#if status === 'working'}
						<Loader2 size={11} strokeWidth={2.6} class="spin" />
					{:else if status === 'await'}
						<AlertCircle size={11} strokeWidth={2.2} />
					{:else if status === 'done'}
						<Check size={11} strokeWidth={2.6} />
					{/if}
					{statusLabel}
				</span>
				<span class="dot" aria-hidden="true"></span>
				<span class="timer" class:live>{timer}</span>
			</span>

			<span class="l2">{title}</span>

			<span class="l3">
				<span class="seg">
					{#if session.is_remote}
						<Cloud size={12} strokeWidth={1.9} />
					{:else}
						<Monitor size={12} strokeWidth={1.9} />
					{/if}
					<span class="dev">{device}</span>
				</span>
				{#if branch}
					<span class="seg">
						<GitBranch size={12} strokeWidth={1.9} />
						<span class="branch">{branch}</span>
					</span>
				{/if}
			</span>
		{/if}
	</div>

	<span
		class="card-actions"
		aria-label="Session actions"
		aria-hidden={swipeLayout && !actionsOpen ? 'true' : undefined}
		inert={swipeLayout && !actionsOpen}
	>
		{#if session.is_active}
			<button
				class="row-action state-toggle"
				title="Settle session"
				aria-label="Settle session"
				onclick={toggleSettled}
			>
				<Check size={16} strokeWidth={1.8} />
			</button>
		{:else}
			<button
				class="row-action state-toggle"
				title="Move to Active"
				aria-label="Move session to Active"
				onclick={toggleSettled}
			>
				<Undo2 size={16} strokeWidth={1.8} />
			</button>
		{/if}
	</span>
</div>

<style>
	.scard-frame {
		--ram-reveal: 0;
		position: relative;
		width: 100%;
		border-radius: 9px;
	}
	.scard-frame:hover,
	.scard-frame:focus-within,
	.scard-frame.actions-open {
		--ram-reveal: 1;
	}
	.scard {
		display: block;
		width: 100%;
		text-align: left;
		padding: 7px 8px 8px;
		border: none;
		border-radius: 9px;
		background: transparent;
		color: inherit;
		cursor: pointer;
		transition: background-color 0.13s ease;
		position: relative;
		z-index: 2;
	}
	.scard-frame:hover .scard,
	.scard-frame:focus-within .scard,
	.scard-frame.actions-open .scard {
		background: var(--sidebar-accent);
	}
	.scard.active { background: var(--sidebar-accent-active); }
	.scard.active::before {
		content: '';
		position: absolute;
		left: 0;
		top: 8px;
		bottom: 8px;
		width: 2px;
		border-radius: 2px;
		background: var(--primary);
		box-shadow: 0 0 8px -1px var(--glow);
	}
	.scard:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	/* Compact two-line row for settled/archived sessions: title + age, then a
	   muted meta line (project · device · branch). Still denser than the
	   three-line active cards. The timer still swaps to the quick action on
	   hover, and the mobile swipe strip is untouched. */
	.scard.compact { padding: 6px 8px 7px; }
	.l1.cline { display: flex; align-items: center; gap: 8px; }
	.cproj {
		color: var(--sidebar-muted);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		max-width: 96px;
	}
	.scard.compact .l3 { margin-top: 3px; font-size: 10.5px; }
	.ctitle {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 13px;
		font-weight: 500;
		letter-spacing: -0.008em;
		color: var(--sidebar-foreground);
	}
	.scard.compact .timer { flex: none; }
	.scard.compact.active::before { top: 6px; bottom: 6px; }

	.l1 {
		display: flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
		overflow: hidden;
	}
	.proj {
		font-size: 12px;
		color: var(--sidebar-muted);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		max-width: 96px;
	}
	.dot {
		width: 2px;
		height: 2px;
		border-radius: 50%;
		background: var(--subtle-foreground);
		flex: none;
	}
	.status {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-size: 12px;
		white-space: nowrap;
		flex: none;
	}
	.status.working { color: var(--primary); }
	.status.await { color: var(--amber); }
	.status.done { color: var(--muted-foreground); }
	.status.idle { color: var(--subtle-foreground); }
	.status :global(svg) { flex: none; }
	.status.working :global(svg) {
		filter: drop-shadow(0 0 4px var(--glow));
		animation: card-pulse 2.4s ease-in-out infinite;
	}
	.status.await :global(svg) { animation: card-pulse 2.4s ease-in-out infinite; }
	.status :global(.spin) { animation: card-spin 1.4s linear infinite; }

	@keyframes card-spin {
		to { transform: rotate(360deg); }
	}
	@keyframes card-pulse {
		0%, 100% { opacity: 0.75; }
		50% { opacity: 1; }
	}

	.timer {
		flex: none;
		font-family: var(--font-mono);
		font-variant-numeric: tabular-nums;
		font-size: 11.5px;
		color: var(--sidebar-muted);
		white-space: nowrap;
		transition: opacity 120ms ease;
	}
	.timer.live { color: var(--primary); }

	/* Desktop swap: the timer yields its slot to the settle/move action on
	   hover, so the quick action appears in place instead of over line 1. */
	.scard-frame:hover .timer,
	.scard-frame:focus-within .timer,
	.scard-frame.actions-open .timer { opacity: 0; }

	/* Receded rows dim their project + title + meta but keep the colored status
	   and timer lit; hover (desktop) restores the whole row. */
	.scard.receded .proj,
	.scard.receded .l2,
	.scard.receded .l3 { opacity: 0.62; transition: opacity 0.13s ease; }
	.scard-frame:hover .scard.receded .proj,
	.scard-frame:hover .scard.receded .l2,
	.scard-frame:hover .scard.receded .l3,
	.scard-frame:focus-within .scard.receded .proj,
	.scard-frame:focus-within .scard.receded .l2,
	.scard-frame:focus-within .scard.receded .l3 { opacity: 1; }

	.l2 {
		display: block;
		margin-top: 3px;
		font-size: 13.5px;
		font-weight: 500;
		letter-spacing: -0.008em;
		color: var(--sidebar-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.l3 {
		margin-top: 4px;
		display: flex;
		align-items: center;
		gap: 5px;
		font-size: 11px;
		color: var(--sidebar-muted);
		min-width: 0;
		flex-wrap: nowrap;
		overflow: hidden;
	}
	.l3 :global(svg) { color: var(--subtle-foreground); flex: none; }
	.seg {
		display: inline-flex;
		align-items: center;
		gap: 3.5px;
		min-width: 0;
		flex: none;
		white-space: nowrap;
	}
	.dev {
		display: block;
		min-width: 0;
		max-width: 96px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.branch {
		max-width: 78px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: var(--font-mono);
		font-size: 10px;
	}

	.card-actions {
		position: absolute;
		top: 4px;
		right: 6px;
		bottom: auto;
		display: flex;
		align-items: center;
		gap: 1px;
		opacity: var(--ram-reveal);
		transition: opacity 120ms ease;
		z-index: 5;
	}
	.card-actions > * {
		transition: color 120ms ease, background-color 120ms ease;
	}
	.row-action {
		width: 22px;
		height: 22px;
		padding: 0;
		border: none;
		border-radius: 6px;
		background: transparent;
		color: var(--sidebar-muted);
		display: grid;
		place-items: center;
		cursor: pointer;
		transition: color 0.12s ease, background-color 0.12s ease, opacity 0.12s ease;
	}
	.row-action:hover {
		color: var(--foreground);
		background: color-mix(in oklch, var(--sidebar-accent-active) 78%, transparent);
	}
	.row-action.state-toggle { color: var(--muted-foreground); }
	.row-action:disabled { opacity: 0.45; cursor: not-allowed; }
	.row-action:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	@media (hover: none), (max-width: 1080px) {
		.scard-frame {
			overflow: hidden;
			touch-action: pan-y;
			background: color-mix(in oklch, var(--sidebar-accent-active) 84%, var(--sidebar));
		}
		/* No hover on touch: keep the timer lit (the swipe strip is the action
		   affordance here, not the in-slot desktop swap). */
		.scard-frame:hover .timer,
		.scard-frame:focus-within .timer,
		.scard-frame.actions-open .timer { opacity: 1; }
		.scard {
			background: var(--sidebar);
			transition: transform 220ms cubic-bezier(0.22, 1, 0.36, 1), background-color 130ms ease;
			will-change: transform;
		}
		/* The swipe track sits behind the card, so the card surface has to be
		   opaque or the revealed actions bleed through the accent tint. */
		.scard.active {
			background:
				linear-gradient(var(--sidebar-accent-active), var(--sidebar-accent-active)),
				var(--sidebar);
		}
		.scard-frame.actions-open .scard { transform: translateX(-44px); }
		.scard-frame.dragging .scard { transition: none; }
		.scard-frame:hover .scard { background: var(--sidebar); }
		.scard-frame:hover .scard.active {
			background:
				linear-gradient(var(--sidebar-accent-active), var(--sidebar-accent-active)),
				var(--sidebar);
		}

		.card-actions {
			top: 0;
			right: 0;
			bottom: 0;
			width: 44px;
			justify-content: flex-end;
			gap: 0;
			padding-left: 0;
			background: none;
			opacity: 1;
			z-index: 1;
			pointer-events: none;
		}
		.scard-frame.actions-open .card-actions { pointer-events: auto; }
		.card-actions > * {
			width: 44px;
			height: 100%;
			min-height: 44px;
			border-radius: 0;
		}
		.card-actions > *:hover {
			background: color-mix(in oklch, var(--sidebar-accent-active) 88%, transparent);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.status :global(svg),
		.status :global(.spin) { animation: none; }
		.scard { transition: none; }
	}
</style>
