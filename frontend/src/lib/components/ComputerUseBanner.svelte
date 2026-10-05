<script lang="ts">
	/**
	 * Computer-use status bar: a compact bar shown while an agent controls the
	 * host desktop. It draws in the shared notification dock (#1823), under the
	 * page header and centred on the chat column, so it never lands under the
	 * desktop app's window controls or over the composer. Hiding it shrinks it
	 * in place to a chip that keeps Stop within reach; it does not move.
	 */
	import { ChevronDown, ChevronUp, Square, X } from 'lucide-svelte';
	import { computerUse } from '$lib/stores/computerUse.svelte';
	import NotificationSlot from './NotificationSlot.svelte';
	import { DOCK_ORDER } from '$lib/stores/notification-dock.svelte';

	$effect(() => {
		computerUse.start();
		return () => computerUse.stop();
	});
	let dismissed = $state(false);
	let collapsed = $state(false);
	$effect(() => {
		if (computerUse.active) dismissed = false;
		else collapsed = false;
	});
	const stopped = $derived(['user', 'escape', 'disabled', 'pointer_moved'].includes(computerUse.stopReason ?? ''));
	const actions: Record<string, string> = {
		screenshot: 'Looking at screen', zoom: 'Reading detail', mouse_move: 'Moving pointer',
		left_click: 'Clicking', right_click: 'Right-clicking', middle_click: 'Middle-clicking',
		double_click: 'Double-clicking', triple_click: 'Triple-clicking', left_click_drag: 'Dragging',
		left_mouse_down: 'Holding mouse button', left_mouse_up: 'Releasing mouse button',
		type: 'Typing', key: 'Pressing keys', hold_key: 'Holding keys', scroll: 'Scrolling',
		wait: 'Waiting', open_application: 'Opening an app', switch_display: 'Screen selected',
		list_windows: 'Listing windows', focus_window: 'Switching windows', window_control: 'Arranging a window',
		read_clipboard: 'Reading clipboard', write_clipboard: 'Writing clipboard'
	};
	const detail = $derived(computerUse.connectionLost ? 'Connection lost — status may be out of date'
		: !computerUse.busy && (computerUse.secondsSince ?? 0) > 2 ? 'Thinking · computer control is still active'
		: actions[computerUse.action ?? ''] ?? 'Using computer');
	const hint = $derived(`Shared mouse & keyboard · ${computerUse.escapeAvailable ? 'Esc on host to stop' : 'Use Stop computer use to take control'}${!computerUse.overlayAvailable ? ' · Screen overlay unavailable' : ''}`);
</script>

{#if computerUse.active}
	<NotificationSlot order={DOCK_ORDER.computerUse}>
	<aside class="cu-bar" class:collapsed aria-label="Computer use controls" data-testid="computer-use-banner">
		<span class="cu-dot" class:lost={computerUse.connectionLost} aria-hidden="true"></span>
		{#if collapsed}
			<strong class="cu-title">Computer use</strong>
		{:else}
			<div class="cu-copy" role="status">
				<div class="cu-line">
					<strong class="cu-title">Computer use</strong>
					<span class="cu-target">{computerUse.displayLabel ?? 'Host desktop'}</span>
					<span class="cu-sep" aria-hidden="true">·</span>
					<span class="cu-action">{detail}</span>
				</div>
				<small class="cu-hint">{hint}</small>
			</div>
		{/if}
		<button class="cu-stop" onclick={() => computerUse.halt()} disabled={computerUse.stopping}>
			<Square size={10} fill="currentColor" aria-hidden="true" />{computerUse.stopping ? 'Stopping…' : 'Stop computer use'}
		</button>
		<button class="cu-icon" aria-label={collapsed ? 'Show computer use details' : 'Hide computer use message'} title={collapsed ? 'Show details' : 'Hide details (computer use continues)'} aria-expanded={!collapsed} onclick={() => collapsed = !collapsed}>
			{#if collapsed}<ChevronDown size={15} aria-hidden="true" />{:else}<ChevronUp size={15} aria-hidden="true" />{/if}
		</button>
	</aside>
	</NotificationSlot>
{:else if stopped && !dismissed}
	<NotificationSlot order={DOCK_ORDER.computerUse}>
	<aside class="cu-bar stopped" aria-label="Computer use stopped">
		<span class="cu-dot idle" aria-hidden="true"></span>
		<div class="cu-copy" role="status">
			<strong class="cu-title">Computer use stopped</strong>
			<small class="cu-hint">You have control. Start a new chat turn to use the computer again.</small>
		</div>
		<button class="cu-icon" aria-label="Dismiss computer use status" onclick={() => dismissed = true}><X size={15} aria-hidden="true" /></button>
	</aside>
	</NotificationSlot>
{/if}

<style>
	/* Agent-control accent, shared with the on-screen overlay and agent cursor. */
	.cu-bar {
		--cu-accent: #8b6cff;
		/* Placed by the notification dock; no position of its own. */
		display: flex;
		align-items: center;
		gap: 10px;
		width: max-content;
		max-width: 100%;
		padding: 6px 6px 6px 12px;
		border: 1px solid color-mix(in oklab, var(--cu-accent) 55%, var(--border-strong, #444));
		border-radius: 12px;
		background: var(--popover, #18181b);
		color: var(--popover-foreground, #f4f4f5);
		box-shadow: var(--shadow-l, 0 8px 24px #0005);
		font-size: 12px;
		line-height: 1.3;
	}
	.cu-bar.collapsed { gap: 8px; padding: 4px 4px 4px 10px; border-radius: 999px; }
	.cu-bar.stopped { border-color: var(--border-strong, #444); }

	.cu-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--cu-accent); box-shadow: 0 0 0 0 color-mix(in oklab, var(--cu-accent) 60%, transparent); animation: cu-pulse 1.8s ease-out infinite; }
	.cu-dot.lost { background: var(--muted-foreground, #999); animation: none; }
	.cu-dot.idle { background: var(--muted-foreground, #999); animation: none; }
	@keyframes cu-pulse { 70% { box-shadow: 0 0 0 7px transparent; } 100% { box-shadow: 0 0 0 0 transparent; } }
	@media (prefers-reduced-motion: reduce) { .cu-dot { animation: none; } }

	.cu-copy { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
	.cu-line { display: flex; align-items: baseline; gap: 6px; min-width: 0; white-space: nowrap; }
	.cu-title { font-weight: 600; font-size: 12.5px; white-space: nowrap; }
	.cu-target { color: var(--muted-foreground, #a1a1aa); overflow: hidden; text-overflow: ellipsis; }
	.cu-sep { color: var(--muted-foreground, #a1a1aa); }
	.cu-action { overflow: hidden; text-overflow: ellipsis; }
	.cu-hint { color: var(--muted-foreground, #a1a1aa); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

	button { display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; cursor: pointer; border-radius: 8px; }
	button:focus-visible { outline: 2px solid var(--cu-accent, #8b6cff); outline-offset: 2px; }
	.cu-stop { gap: 6px; height: 28px; padding: 0 10px; background: #7c3aed; color: #fff; font-size: 12px; font-weight: 600; }
	.cu-stop:hover { background: #6d28d9; }
	.cu-stop:disabled { opacity: 0.65; cursor: wait; }
	.collapsed .cu-stop { border-radius: 999px; }
	.cu-icon { width: 28px; height: 28px; color: var(--muted-foreground, #a1a1aa); }
	.cu-icon:hover { background: var(--accent, #27272a); color: var(--foreground, #f4f4f5); }
	.collapsed .cu-icon { border-radius: 999px; }

	@media (max-width: 640px) {
		.cu-bar:not(.collapsed) { flex-wrap: wrap; row-gap: 6px; }
		.cu-bar:not(.collapsed) .cu-copy { flex: 1 1 calc(100% - 60px); }
		.cu-bar:not(.collapsed) .cu-line { flex-wrap: wrap; white-space: normal; }
		.cu-bar:not(.collapsed) .cu-stop { margin-left: 18px; }
	}
</style>
