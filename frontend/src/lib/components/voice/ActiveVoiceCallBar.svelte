<script lang="ts">
	/**
	 * The call bar shown while a voice call runs in another chat or page.
	 *
	 * Docked (the default) it draws in the notification dock under the page
	 * header. Dragging its grip, or moving the grip with the arrow keys, floats
	 * it anywhere in the window; the spot is saved on this device as a fraction
	 * of the free space (voiceCallBarPosition.ts), so CSS keeps it on screen
	 * whatever the window size. Double-clicking the grip, Escape on the grip,
	 * or the put-back button docks it again.
	 *
	 * In the desktop app it can also pop out into its own always-on-top window
	 * (desktop/src/main/call-popout.ts). This page keeps the call and the audio:
	 * the window only mirrors the state below and sends button presses back.
	 */
	import { onMount, tick } from 'svelte';
	import { Mic, MicOff, PhoneOff, Volume2, VolumeX, ArrowUpRight, GripVertical, PictureInPicture2, Undo2 } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { layout } from '$lib/stores/layout.svelte';
	import { sessions } from '$lib/stores/app-data.svelte';
	import NotificationSlot from '$lib/components/NotificationSlot.svelte';
	import { DOCK_ORDER } from '$lib/stores/notification-dock.svelte';
	import { voiceCallState, isCallSurfaceActive, toggleMic, toggleAgent, hangup, reconcileVoiceCall, MIC_PROBLEM_LABEL } from '$lib/services/voiceCall';
	import { loadCallBarPosition, saveCallBarPosition, toFraction, type CallBarPosition } from '$lib/services/voiceCallBarPosition';
	import { desktop, type DesktopCallState } from '$lib/services/desktop';

	let { onReturn }: { onReturn: (sessionId: string) => void } = $props();
	const call = $derived($voiceCallState);
	const owner = $derived($sessions.find(s => s.id === call.sessionId));
	const micProblem = $derived(call.status === 'live' && !call.micMuted && call.micProblem);
	const active = $derived(isCallSurfaceActive(call, call.sessionId));
	const away = $derived($activeSession.sessionId !== call.sessionId || $layout.activeView?.type !== 'session');
	const label = $derived(micProblem ? MIC_PROBLEM_LABEL : `Voice call · ${call.status}`);
	const title = $derived(owner?.title || 'Voice session');

	const popout = desktop?.voiceCall ?? null;
	let poppedOut = $state(false);
	const visible = $derived(active && away && !poppedOut);

	let position = $state<CallBarPosition | null>(loadCallBarPosition());
	let dragging = $state(false);
	const floating = $derived(position !== null);
	let card = $state<HTMLElement>();
	let bounds = $state<HTMLElement>();
	let grip = $state<HTMLElement>();
	let grab: { dx: number; dy: number; width: number; height: number } | null = null;

	/** The spot that puts the card's top-left corner at (left, top), inside the allowed area. */
	function placeAt(left: number, top: number, width: number, height: number): CallBarPosition | null {
		if (!bounds) return null;
		const area = bounds.getBoundingClientRect();
		return { x: toFraction(left, width, area.left, area.width), y: toFraction(top, height, area.top, area.height) };
	}

	async function refocusGrip() {
		await tick();
		grip?.focus();
	}

	/** A press on the grip; it becomes a drag once the pointer travels a few pixels, so a click leaves the bar where it is. */
	let press: { x: number; y: number } | null = null;

	function startDrag(event: PointerEvent) {
		if (event.button !== 0 || !card) return;
		event.preventDefault();
		const rect = card.getBoundingClientRect();
		press = { x: event.clientX, y: event.clientY };
		grab = { dx: event.clientX - rect.left, dy: event.clientY - rect.top, width: rect.width, height: rect.height };
		window.addEventListener('pointermove', moveDrag);
		window.addEventListener('pointerup', endDrag);
		window.addEventListener('pointercancel', endDrag);
	}

	function moveDrag(event: PointerEvent) {
		if (!grab || !press) return;
		if (!dragging && Math.hypot(event.clientX - press.x, event.clientY - press.y) < 4) return;
		dragging = true;
		position = placeAt(event.clientX - grab.dx, event.clientY - grab.dy, grab.width, grab.height) ?? position;
	}

	function endDrag() {
		window.removeEventListener('pointermove', moveDrag);
		window.removeEventListener('pointerup', endDrag);
		window.removeEventListener('pointercancel', endDrag);
		const moved = dragging;
		grab = null;
		press = null;
		dragging = false;
		if (moved) saveCallBarPosition(position);
	}

	function dock() {
		position = null;
		saveCallBarPosition(null);
		void refocusGrip();
	}

	const STEPS: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };

	function gripKey(event: KeyboardEvent) {
		if (event.key === 'Escape' || event.key === 'Home') {
			if (!floating) return;
			event.preventDefault();
			dock();
			return;
		}
		const step = STEPS[event.key];
		if (!step || !card) return;
		event.preventDefault();
		const by = event.shiftKey ? 64 : 16;
		const rect = card.getBoundingClientRect();
		const next = placeAt(rect.left + step[0] * by, rect.top + step[1] * by, rect.width, rect.height);
		if (!next) return;
		const wasDocked = !floating;
		position = next;
		saveCallBarPosition(next);
		// The grip is drawn anew outside the dock; keep the keyboard on it.
		if (wasDocked) void refocusGrip();
	}

	function snapshot(): DesktopCallState {
		return { label, title, micMuted: !!call.micMuted, agentMuted: !!call.agentMuted, micProblem: !!micProblem };
	}

	async function popOut() {
		if (!popout || !call.sessionId) return;
		poppedOut = (await popout.open(snapshot()).catch(() => false)) === true;
	}

	// Keep the popped-out window in step with the call; close it when the call ends.
	$effect(() => {
		if (!popout || !poppedOut) return;
		if (!active) {
			popout.close();
			poppedOut = false;
			return;
		}
		popout.update(snapshot());
	});

	onMount(() => {
		const reconcile = () => {
			if (document.visibilityState === 'visible') void reconcileVoiceCall();
		};
		document.addEventListener('visibilitychange', reconcile);
		window.addEventListener('pageshow', reconcile);
		const offCommand = popout?.onCommand((command) => {
			const sessionId = $voiceCallState.sessionId;
			if (command === 'toggle-mic') toggleMic();
			else if (command === 'toggle-agent') toggleAgent();
			else if (command === 'hangup') void hangup();
			else if (command === 'return' && sessionId) onReturn(sessionId);
		});
		const offClosed = popout?.onClosed(() => { poppedOut = false; });
		return () => {
			document.removeEventListener('visibilitychange', reconcile);
			window.removeEventListener('pageshow', reconcile);
			endDrag();
			offCommand?.();
			offClosed?.();
			// Nothing here would answer the pop-out's buttons any more.
			if (poppedOut) popout?.close();
		};
	});
</script>

{#snippet bar()}
	<aside class="active-call" class:dragging aria-label="Active voice call" data-testid="active-voice-call-bar" bind:this={card}>
		<button
			class="grip"
			bind:this={grip}
			aria-label="Move call bar"
			aria-keyshortcuts="ArrowUp ArrowDown ArrowLeft ArrowRight Escape"
			title={floating ? 'Drag to move · double-click to put back' : 'Drag to move'}
			onpointerdown={startDrag}
			ondblclick={() => floating && dock()}
			onkeydown={gripKey}
		><GripVertical size={16} /></button>
		<button class="return" onclick={() => call.sessionId && onReturn(call.sessionId)} title="Return to voice conversation">
			<span><strong class:mic-problem={micProblem}>{label}</strong><span class="owner">{title}</span></span>
			<ArrowUpRight size={16} />
		</button>
		<button aria-label={call.micMuted ? 'Unmute microphone' : 'Mute microphone'} aria-pressed={call.micMuted} onclick={toggleMic}>
			{#if call.micMuted}<MicOff size={18} />{:else}<Mic size={18} />{/if}
		</button>
		<button aria-label={call.agentMuted ? 'Unmute speaker' : 'Mute speaker'} aria-pressed={call.agentMuted} onclick={toggleAgent}>
			{#if call.agentMuted}<VolumeX size={18} />{:else}<Volume2 size={18} />{/if}
		</button>
		{#if popout}
			<button aria-label="Pop out call controls" title="Keep the call controls on top of other apps" onclick={popOut}><PictureInPicture2 size={18} /></button>
		{/if}
		{#if floating}
			<button class="put-back" aria-label="Put the call bar back" title="Put the call bar back" onclick={dock}><Undo2 size={16} /></button>
		{/if}
		<button class="end" aria-label="End voice call" onclick={() => hangup()}><PhoneOff size={18} /></button>
	</aside>
{/snippet}

{#if visible && call.sessionId}
	<!-- The area a moved bar may cover: below the desktop title strip and inside the phone's safe areas. -->
	<div class="call-bounds" bind:this={bounds} aria-hidden="true"></div>
	{#if position}
		<div class="call-float" style:--x={position.x} style:--y={position.y}>{@render bar()}</div>
	{:else}
		<NotificationSlot order={DOCK_ORDER.voiceCall}>{@render bar()}</NotificationSlot>
	{/if}
{/if}

<style>
	/* Placed by the notification dock (#1823) until moved: it used to sit 12px from the window top, inside the desktop title bar's drag strip. */
	.active-call { display: flex; align-items: center; gap: 4px; max-width: 100%; padding: 6px; border: 1px solid var(--primary); border-radius: var(--radius); background: var(--background); color: var(--foreground); box-shadow: var(--shadow-l); }
	button { display: flex; align-items: center; justify-content: center; flex-shrink: 0; min-width: 40px; min-height: 40px; padding: 8px; border: 0; border-radius: var(--radius); background: transparent; color: inherit; cursor: pointer; }
	button:hover, button[aria-pressed='true'] { background: var(--muted); }
	button:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
	.grip { min-width: 28px; padding: 8px 2px; color: var(--muted-foreground); cursor: grab; touch-action: none; user-select: none; -webkit-user-select: none; }
	.grip:hover { color: var(--foreground); }
	.dragging, .dragging .grip { cursor: grabbing; }
	.dragging { user-select: none; -webkit-user-select: none; box-shadow: var(--shadow-l), 0 0 0 3px color-mix(in oklab, var(--primary) 25%, transparent); }
	.return { flex: 1; gap: 12px; min-width: 0; text-align: left; }
	.return > span { min-width: 0; }
	strong, .owner { display: block; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 260px; }
	.owner { color: var(--muted-foreground); max-width: min(260px, 35vw); }
	.put-back { min-width: 32px; color: var(--muted-foreground); }
	.put-back:hover { color: var(--foreground); }
	.end { color: var(--destructive); }
	.mic-problem { color: var(--warning); }

	/* Moved by the user. The fraction is of the free space: CSS keeps the card
	   inside the bounds at any window size, so a resize never strands it. */
	.call-bounds {
		--bar-top: calc(var(--titlebar-height, 0px) + max(8px, env(safe-area-inset-top)));
		--bar-bottom: max(8px, env(safe-area-inset-bottom));
		position: fixed;
		inset: var(--bar-top) 8px var(--bar-bottom);
		visibility: hidden;
		pointer-events: none;
	}
	.call-float {
		--bar-top: calc(var(--titlebar-height, 0px) + max(8px, env(safe-area-inset-top)));
		--bar-bottom: max(8px, env(safe-area-inset-bottom));
		position: fixed;
		left: calc(8px + (100vw - 16px) * var(--x));
		top: calc(var(--bar-top) + (100dvh - var(--bar-top) - var(--bar-bottom)) * var(--y));
		translate: calc(var(--x) * -100%) calc(var(--y) * -100%);
		max-width: calc(100vw - 16px);
		z-index: 150;
		-webkit-app-region: no-drag;
	}
	.call-float > .active-call { width: max-content; max-width: min(640px, calc(100vw - 16px)); animation: call-in 160ms ease-out; }
	@keyframes call-in { from { opacity: 0; } to { opacity: 1; } }
	@media (prefers-reduced-motion: reduce) { .call-float > .active-call { animation: none; } }
</style>
