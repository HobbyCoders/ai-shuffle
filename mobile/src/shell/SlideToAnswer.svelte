<script lang="ts">
	/**
	 * Slide to answer: a knob in a rail. Sliding it right approves, left denies;
	 * it follows the finger and springs back short of the end. Deny and the
	 * approve label at the rail's ends are ordinary buttons, so a tap answers
	 * too, and the knob takes the arrow keys.
	 *
	 * `armed` holds back taps for a moment after the request appears (a tap
	 * meant for something else must not answer it), the same rule as the
	 * desktop permission prompt.
	 */
	import { Check, ChevronsLeftRight, X } from 'lucide-svelte';
	import { knobFraction, slideAnswer } from './quick-controls';

	interface Props {
		approveLabel?: string;
		disabled?: boolean;
		armed?: boolean;
		onApprove: () => void;
		onDeny: () => void;
	}

	let { approveLabel = 'Approve', disabled = false, armed = true, onApprove, onDeny }: Props = $props();

	let railRef = $state<HTMLElement | null>(null);
	let knobRef = $state<HTMLElement | null>(null);
	/** -1 (deny) … 1 (approve). */
	let fraction = $state(0);
	let dragging = $state(false);
	let dragId = -1;
	let startX = 0;
	let travel = $state(0);

	const verdict = $derived(slideAnswer(fraction));

	function railTravel(): number {
		const rail = railRef?.clientWidth ?? 0;
		const knob = knobRef?.offsetWidth ?? 48;
		return Math.max(1, rail / 2 - knob / 2 - 4);
	}

	function finish(answer: 'approve' | 'deny' | null) {
		if (answer === 'approve') onApprove();
		else if (answer === 'deny') onDeny();
		fraction = 0;
	}

	function pointerDown(event: PointerEvent) {
		if (disabled || event.button !== 0) return;
		dragId = event.pointerId;
		startX = event.clientX;
		travel = railTravel();
		dragging = true;
		try { knobRef?.setPointerCapture?.(event.pointerId); } catch { /* synthetic pointer */ }
		event.preventDefault();
	}

	function pointerMove(event: PointerEvent) {
		if (event.pointerId !== dragId) return;
		fraction = knobFraction(event.clientX - startX, travel);
	}

	function pointerUp(event: PointerEvent) {
		if (event.pointerId !== dragId) return;
		dragId = -1;
		dragging = false;
		const answer = event.type === 'pointercancel' ? null : slideAnswer(fraction);
		if (answer) navigator.vibrate?.(answer === 'approve' ? 16 : 10);
		finish(answer);
	}

	function keydown(event: KeyboardEvent) {
		if (disabled) return;
		if (event.key === 'ArrowRight') fraction = Math.min(1, fraction + 0.5);
		else if (event.key === 'ArrowLeft') fraction = Math.max(-1, fraction - 0.5);
		else if (event.key === 'Escape' && fraction !== 0) fraction = 0;
		else return;
		event.preventDefault();
		event.stopPropagation();
		if (Math.abs(fraction) >= 1) finish(slideAnswer(fraction));
	}

	function tap(answer: 'approve' | 'deny') {
		if (disabled || !armed) return;
		finish(answer);
	}
</script>

<div
	bind:this={railRef}
	class="rail"
	class:arm-approve={verdict === 'approve'}
	class:arm-deny={verdict === 'deny'}
	class:disabled
	role="group"
	aria-label={`Slide right to ${approveLabel.toLowerCase()}, left to deny`}
	data-no-drawer-swipe
	style:--fr={Math.max(0, fraction)}
	style:--fl={Math.max(0, -fraction)}
>
	<span class="fill l" aria-hidden="true"></span>
	<span class="fill r" aria-hidden="true"></span>
	<button type="button" class="rail-end deny" {disabled} onclick={() => tap('deny')}><X size={16} aria-hidden="true" />Deny</button>
	<button type="button" class="rail-end approve" {disabled} onclick={() => tap('approve')}>{approveLabel}<Check size={16} aria-hidden="true" /></button>
	<div
		bind:this={knobRef}
		class="knob"
		class:dragging
		role="slider"
		tabindex={disabled ? -1 : 0}
		aria-label={`Slide right to ${approveLabel.toLowerCase()}, left to deny`}
		aria-valuemin={-1}
		aria-valuemax={1}
		aria-valuenow={Number(fraction.toFixed(2))}
		aria-disabled={disabled || undefined}
		style:transform={`translateX(${fraction * (travel || railTravel())}px)`}
		onpointerdown={pointerDown}
		onpointermove={pointerMove}
		onpointerup={pointerUp}
		onpointercancel={pointerUp}
		onkeydown={keydown}
	>
		{#if verdict === 'approve'}<Check size={20} aria-hidden="true" />{:else if verdict === 'deny'}<X size={20} aria-hidden="true" />{:else}<ChevronsLeftRight size={20} aria-hidden="true" />{/if}
	</div>
</div>

<style>
	.rail {
		--fr: 0;
		--fl: 0;
		position: relative;
		height: 56px;
		margin-top: 4px;
		border-radius: 28px;
		background: var(--muted);
		box-shadow: inset 0 0 0 1px var(--border-subtle);
		overflow: hidden;
		touch-action: none;
		user-select: none;
		-webkit-user-select: none;
	}
	.rail.disabled { opacity: 0.55; }
	.fill { position: absolute; top: 0; bottom: 0; pointer-events: none; }
	.fill.r { left: 50%; width: calc(var(--fr) * 50%); background: color-mix(in oklch, var(--success, var(--primary)) 16%, transparent); }
	.fill.l { right: 50%; width: calc(var(--fl) * 50%); background: color-mix(in oklch, var(--destructive) 16%, transparent); }
	.rail-end {
		position: absolute;
		top: 4px;
		bottom: 4px;
		z-index: 1;
		display: flex;
		align-items: center;
		gap: 6px;
		min-width: 44px;
		padding: 0 16px;
		border: 0;
		border-radius: 24px;
		background: transparent;
		font: inherit;
		font-size: 14px;
		font-weight: 600;
		cursor: pointer;
		transition: background-color 0.15s, color 0.15s;
	}
	.rail-end.deny { left: 4px; color: var(--destructive); }
	.rail-end.approve { right: 4px; color: var(--success, var(--primary)); }
	.rail-end:hover:not(:disabled) { background: var(--hover-overlay); }
	.rail-end:disabled { cursor: not-allowed; }
	.rail.arm-approve .rail-end.approve { background: var(--success, var(--primary)); color: var(--success-foreground, var(--primary-foreground)); }
	.rail.arm-deny .rail-end.deny { background: var(--destructive); color: var(--destructive-foreground); }
	.knob {
		position: absolute;
		top: 4px;
		left: calc(50% - 24px);
		z-index: 2;
		display: grid;
		place-items: center;
		width: 48px;
		height: 48px;
		border: 1px solid var(--border-strong);
		border-radius: 50%;
		background: var(--popover);
		color: var(--foreground);
		box-shadow: var(--shadow-m);
		cursor: grab;
		touch-action: none;
		transition: transform 260ms cubic-bezier(0.34, 1.4, 0.64, 1);
	}
	.knob.dragging { cursor: grabbing; transition: none; }
	.knob:focus-visible, .rail-end:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	@media (prefers-reduced-motion: reduce) {
		.knob, .rail-end { transition: none; }
	}
</style>
