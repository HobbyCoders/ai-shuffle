<script lang="ts">
	import { Monitor, MousePointer2, Square, X } from 'lucide-svelte';
	import { computerUse } from '$lib/stores/computerUse.svelte';

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
		read_clipboard: 'Reading clipboard', write_clipboard: 'Writing clipboard'
	};
	const detail = $derived(computerUse.connectionLost ? 'Connection lost — status may be out of date'
		: !computerUse.busy && (computerUse.secondsSince ?? 0) > 2 ? 'Thinking · computer control is still active'
		: actions[computerUse.action ?? ''] ?? 'Using computer');
	const pointer = $derived.by(() => {
		const g = computerUse.geometry, p = computerUse.cursor;
		if (!g || !p || g.width <= 0 || g.height <= 0) return null;
		return { x: Math.max(0, Math.min(100, (p[0] - g.left) / g.width * 100)),
			y: Math.max(0, Math.min(100, (p[1] - g.top) / g.height * 100)) };
	});
</script>

{#if computerUse.active}
	<aside class="computer-control" class:collapsed aria-label="Computer use controls" data-testid="computer-use-banner">
		{#if !collapsed}
			<div class="screen" aria-hidden="true">
				<Monitor size={30} />
				{#if pointer}<span class="pointer" style:left={`${pointer.x}%`} style:top={`${pointer.y}%`}><MousePointer2 size={13} fill="currentColor" /></span>{/if}
			</div>
			<div class="copy" role="status">
				<strong>Computer use <span>· {computerUse.displayLabel ?? 'Host desktop'}</span></strong>
				<span>{detail}</span>
				<small>Shared mouse &amp; keyboard · {computerUse.escapeAvailable ? 'Esc on host to stop' : 'Use Stop computer use to take control'}{!computerUse.overlayAvailable ? ' · Screen overlay unavailable' : ''}</small>
			</div>
		{/if}
		<button class="dismiss" class:restore={collapsed} aria-label={collapsed ? 'Show computer use details' : 'Hide computer use message'} title={collapsed ? 'Show computer use details' : 'Hide message (computer use continues)'} aria-expanded={!collapsed} onclick={() => collapsed = !collapsed}>
			{#if collapsed}<Monitor size={16} aria-hidden="true" /><span>Computer use</span>{:else}<X size={16} aria-hidden="true" />{/if}
		</button>
		<button class="stop" onclick={() => computerUse.halt()} disabled={computerUse.stopping}>
			<Square size={12} fill="currentColor" />{computerUse.stopping ? 'Stopping…' : 'Stop computer use'}
		</button>
	</aside>
{:else if stopped && !dismissed}
	<aside class="computer-control stopped" aria-label="Computer use stopped">
		<Monitor size={22} aria-hidden="true" />
		<div class="copy" role="status"><strong>Computer use stopped</strong><span>You have control. Start a new chat turn to use the computer again.</span></div>
		<button class="dismiss" aria-label="Dismiss computer use status" onclick={() => dismissed = true}><X size={16} /></button>
	</aside>
{/if}

<style>
	.computer-control { position: fixed; z-index: 150; bottom: calc(18px + env(safe-area-inset-bottom)); left: 50%; transform: translateX(-50%); display: flex; align-items: center; gap: 14px; width: max-content; max-width: calc(100vw - 24px); padding: 14px 16px; border: 1px solid #9b86ff; border-radius: 14px; background: var(--background, #141417); color: var(--foreground, #f4f4f5); box-shadow: 0 8px 32px #0004; }
	.screen { position: relative; width: 42px; height: 32px; display: grid; place-items: center; color: #9b86ff; flex-shrink: 0; }
	.pointer { position: absolute; transform: translate(-50%, -50%); }
	.copy { display: flex; flex-direction: column; gap: 3px; font-size: 12px; min-width: 0; }
	.copy strong { font-weight: 650; font-size: 13px; }
	.copy strong span { font-weight: 450; }
	.copy small { opacity: 0.7; font-size: 11px; }
	button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; flex-shrink: 0; cursor: pointer; border-radius: 8px; }
	.stop { background: #7c3aed; color: #fff; font-size: 12px; font-weight: 600; padding: 9px 12px; }
	.stop:hover { background: #6d28d9; }
	.stop:disabled { opacity: 0.65; cursor: wait; }
	button:focus-visible { outline: 2px solid #9b86ff; outline-offset: 3px; }
	.dismiss { padding: 6px; min-width: 32px; min-height: 32px; }
	.collapsed { top: calc(12px + env(safe-area-inset-top)); right: calc(12px + env(safe-area-inset-right)); bottom: auto; left: auto; transform: none; gap: 8px; padding: 6px; }
	.restore { font-size: 12px; }
	.stopped { border-color: var(--border, #666); }
	@media (max-width: 640px) { .computer-control.collapsed { flex-wrap: nowrap; gap: 4px; padding: 6px; } .computer-control { flex-wrap: wrap; gap: 10px; padding: 12px; } .screen { display: none; } .copy { flex: 1 1 200px; } .copy strong span { display: block; } .stop { margin-left: auto; } }
</style>
