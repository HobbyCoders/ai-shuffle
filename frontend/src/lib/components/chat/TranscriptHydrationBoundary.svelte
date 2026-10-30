<script lang="ts" module>
	import type { Readable } from 'svelte/store';
	import type { ActiveSessionState } from '$lib/stores/active-session';

	/** Only transcript descendants read this; the composer/permission trays stay live. */
	export const TRANSCRIPT_PRESENTATION = Symbol('transcript-presentation');
	export const TRANSCRIPT_ACTIONS = Symbol('transcript-actions');
	export interface TranscriptActions extends Readable<boolean> {
		/** Synchronous guard for a click or an asynchronous completion. */
		isCurrent: (sessionId: string | null | undefined) => boolean;
	}
	// Leave headroom above measured 4× short-history hydration. A ready target
	// is published immediately; this delays only the slow-load notice.
	export const TRANSCRIPT_LOADING_GRACE_MS = 350;
	// The recorder's cross-chat flash window is 150 ms; leave 100 ms of margin.
	export const TRANSCRIPT_LOADING_MIN_VISIBLE_MS = 250;
	export type TranscriptPresentation = Readable<ActiveSessionState>;
</script>

<script lang="ts">
	import { onMount, onDestroy, setContext, type Snippet } from 'svelte';
	import { get, writable } from 'svelte/store';
	import { activeSession } from '$lib/stores/active-session';
	import { classifyChatSurface, type ChatSurface } from '$lib/components/layout/chat-empty-state';
	import TranscriptLoadingSkeleton from './TranscriptLoadingSkeleton.svelte';

	let { children, graceMs = TRANSCRIPT_LOADING_GRACE_MS, mirrorPending = false, navigationOverlayOpen = false, onSurface }: {
		children: Snippet;
		graceMs?: number;
		/** The ancestor gate owns inertness and its own notice during a remote barrier. */
		mirrorPending?: boolean;
		/** Native phone navigation selects its target one frame after closing the drawer. */
		navigationOverlayOpen?: boolean;
		onSurface?: (surface: ChatSurface, messageCount: number, sessionId: string | null | undefined) => void;
	} = $props();

	// Build-time rollback to the original immediate loading presentation.
	const enabled = import.meta.env.VITE_PERF_STABLE_TRANSCRIPT_LOADING !== '0';
	const initial = get(activeSession);
	const presented = writable(initial);
	setContext<TranscriptPresentation>(TRANSCRIPT_PRESENTATION, { subscribe: presented.subscribe });
	// One shallow state shell, not a per-session cache or a second rendered tree.
	// Arrays stay owned by the native feed; no whole-history copy on each token.
	let settled = initial.isLoadingSession || initial.isRestoringSession ? null : { ...initial };
	let shown = initial;
	let retaining = $state(false);
	let holdingNotice = $state(false);
	const actionsAllowed = writable(false);
	setContext<TranscriptActions>(TRANSCRIPT_ACTIONS, {
		subscribe: actionsAllowed.subscribe,
		isCurrent(sessionId) {
			const live = get(activeSession);
			return !retaining && !holdingNotice && !mirrorPending && live.sessionId === sessionId
				&& !live.isLoadingSession && !live.isRestoringSession;
		}
	});
	// DOM inertness cannot reach body portals. Descendants close their transient
	// UI from this context; dispatch/completion guards also read the live target.
	$effect(() => actionsAllowed.set(!retaining && !holdingNotice && !mirrorPending
		&& !$presented.isLoadingSession && !$presented.isRestoringSession));
	let targetSessionId = $state(initial.sessionId);
	let graceElapsed = $state(false);
	let noticeHasShown = $state(false);
	let overlayHandoff = $state(false);
	$effect(() => {
		if (navigationOverlayOpen) { overlayHandoff = true; return; }
		if (!overlayHandoff) return;
		// Keep a first notice behind the drawer until its existing native select
		// callback has run. The deadline is not reset and readiness is never delayed.
		const frame = requestAnimationFrame(() => { overlayHandoff = false; });
		return () => cancelAnimationFrame(frame);
	});
	let pendingTarget: string | null = null;
	let epoch = 0;
	let timer: ReturnType<typeof setTimeout> | undefined;
	let removalTimer: ReturnType<typeof setTimeout> | undefined;
	let noticePaintedAt: number | null = null;

	function cancelGrace() {
		epoch++;
		if (timer !== undefined) clearTimeout(timer);
		if (removalTimer !== undefined) clearTimeout(removalTimer);
		timer = removalTimer = undefined;
		pendingTarget = null;
		graceElapsed = false;
		noticeHasShown = false;
		noticePaintedAt = null;
		holdingNotice = false;
	}

	function finishNotice() {
		// Readiness publishes the real transcript immediately. Only a notice
		// already observed on a frame may outlive it, never the old transcript.
		const remaining = noticePaintedAt === null ? 0
			: TRANSCRIPT_LOADING_MIN_VISIBLE_MS - (performance.now() - noticePaintedAt);
		if (remaining <= 0) { cancelGrace(); return; }
		if (timer !== undefined) clearTimeout(timer);
		timer = undefined;
		holdingNotice = true;
		if (removalTimer !== undefined) return;
		const mine = epoch;
		removalTimer = setTimeout(() => {
			if (mine !== epoch) return;
			removalTimer = undefined;
			cancelGrace();
		}, remaining);
	}

	onMount(() => activeSession.subscribe(current => {
		// Navigation is not a load completing: even a just-painted notice must
		// disappear immediately for a cached return, or restart grace for C.
		if (current.sessionId !== targetSessionId) cancelGrace();
		targetSessionId = current.sessionId;
		const loading = current.isLoadingSession || current.isRestoringSession;
		if (!enabled || !loading) {
			if (enabled) finishNotice();
			else cancelGrace();
			retaining = false;
			settled = current.sessionId ? { ...current } : null;
			shown = current;
			presented.set(current);
			return;
		}

		if (removalTimer !== undefined) clearTimeout(removalTimer);
		removalTimer = undefined;
		holdingNotice = false;

		// A rapid A → B → C navigation gets C's own grace; B's timer cannot
		// reveal a skeleton over C, nor fire after C has already been painted.
		const target = current.sessionId ?? 'restoring';
		if (pendingTarget !== target) {
			cancelGrace();
			pendingTarget = target;
			const mine = epoch;
			timer = setTimeout(() => {
				if (mine !== epoch || pendingTarget !== target) return;
				timer = undefined;
				graceElapsed = true;
			}, Math.max(0, graceMs));
		}

		retaining = settled !== null;
		if (settled) {
			// Ignore partial/stale feed publications until this target is ready.
			if (shown !== settled) { shown = settled; presented.set(settled); }
		} else {
			// A cold first visit has no outgoing conversation to retain.
			shown = current;
			presented.set(current);
		}
	}));
	onDestroy(cancelGrace);

	const overlay = $derived((retaining || holdingNotice) && graceElapsed && !mirrorPending
		&& (noticeHasShown || (!navigationOverlayOpen && !overlayHandoff)));
	$effect(() => {
		if (!overlay) return;
		noticeHasShown = true;
		if (noticePaintedAt !== null) return;
		// Start from the notice's first frame, not the possibly long-delayed
		// grace callback. A target ready before this frame needs no minimum hold.
		const frame = requestAnimationFrame(() => { noticePaintedAt = performance.now(); });
		return () => cancelAnimationFrame(frame);
	});
	const messageCount = $derived($presented.messages.length + ($presented.pendingPeerMessages?.length ?? 0));
	$effect(() => onSurface?.(overlay ? 'loading' : classifyChatSurface({
		sessionId: $presented.sessionId,
		messageCount,
		isLoadingSession: $presented.isLoadingSession,
		isRestoringSession: $presented.isRestoringSession
	}), overlay ? 0 : messageCount, overlay ? targetSessionId : $presented.sessionId));
</script>

<div class="transcript-hydration" data-transcript-pending={retaining ? 'true' : undefined}>
	<div class="transcript-presentation" inert={(retaining || holdingNotice) && !mirrorPending} class:is-concealed={overlay}>
		{@render children()}
	</div>
	{#if overlay}
		<div class="hydration-overlay"><TranscriptLoadingSkeleton /></div>
	{/if}
</div>

<style>
	.transcript-hydration, .transcript-presentation {
		display: flex;
		flex: 1;
		min-width: 0;
		min-height: 0;
		flex-direction: column;
	}
	.transcript-hydration { position: relative; }
	/* Keep the old mounted geometry/reader snapshot intact underneath the
	   delayed notice. Publishing a synthetic empty old state would erase it. */
	.is-concealed { visibility: hidden; }
	.hydration-overlay { position: absolute; inset: 0; overflow: hidden; }
</style>
