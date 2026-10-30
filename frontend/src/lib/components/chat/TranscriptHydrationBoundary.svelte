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
	const actionsAllowed = writable(false);
	setContext<TranscriptActions>(TRANSCRIPT_ACTIONS, {
		subscribe: actionsAllowed.subscribe,
		isCurrent(sessionId) {
			const live = get(activeSession);
			return !retaining && !mirrorPending && live.sessionId === sessionId
				&& !live.isLoadingSession && !live.isRestoringSession;
		}
	});
	// DOM inertness cannot reach body portals. Descendants close their transient
	// UI from this context; dispatch/completion guards also read the live target.
	$effect(() => actionsAllowed.set(!retaining && !mirrorPending
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

	function cancelGrace() {
		epoch++;
		if (timer !== undefined) clearTimeout(timer);
		timer = undefined;
		pendingTarget = null;
		graceElapsed = false;
		noticeHasShown = false;
	}

	onMount(() => activeSession.subscribe(current => {
		targetSessionId = current.sessionId;
		const loading = current.isLoadingSession || current.isRestoringSession;
		if (!enabled || !loading) {
			cancelGrace();
			retaining = false;
			settled = current.sessionId ? { ...current } : null;
			shown = current;
			presented.set(current);
			return;
		}

		// A rapid A → B → C navigation gets C's own grace; B's timer cannot
		// reveal a skeleton over C, nor fire after C has already been painted.
		const target = current.sessionId ?? 'restoring';
		if (pendingTarget !== target) {
			// Once a genuine slow load has shown the notice, keep it continuous
			// across another navigation instead of flashing the old rows back in.
			const alreadyShown = noticeHasShown;
			cancelGrace();
			pendingTarget = target;
			graceElapsed = alreadyShown;
			noticeHasShown = alreadyShown;
			if (!alreadyShown) {
				const mine = epoch;
				timer = setTimeout(() => {
					if (mine !== epoch || pendingTarget !== target) return;
					timer = undefined;
					graceElapsed = true;
				}, Math.max(0, graceMs));
			}
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

	const overlay = $derived(retaining && graceElapsed && !mirrorPending
		&& (noticeHasShown || (!navigationOverlayOpen && !overlayHandoff)));
	$effect(() => { if (overlay) noticeHasShown = true; });
	const messageCount = $derived($presented.messages.length + ($presented.pendingPeerMessages?.length ?? 0));
	$effect(() => onSurface?.(overlay ? 'loading' : classifyChatSurface({
		sessionId: $presented.sessionId,
		messageCount,
		isLoadingSession: $presented.isLoadingSession,
		isRestoringSession: $presented.isRestoringSession
	}), overlay ? 0 : messageCount, overlay ? targetSessionId : $presented.sessionId));
</script>

<div class="transcript-hydration" data-transcript-pending={retaining ? 'true' : undefined}>
	<div class="transcript-presentation" inert={retaining && !mirrorPending} class:is-concealed={overlay}>
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
