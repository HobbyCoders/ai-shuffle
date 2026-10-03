<script lang="ts">
	/**
	 * SidebarSheet — the panel's one overlay surface.
	 *
	 * On the phone it is a bottom sheet over a dimmed scrim: drag it down past
	 * 70px (or tap the scrim) to dismiss. On desktop the same content becomes a
	 * small popover; the caller places it with `placement` (the More flyout sits
	 * beside the rail, the filter popover under the filter button), and the
	 * scrim turns transparent so it only catches the outside click.
	 *
	 * The sheet lives inside the sidebar plane rather than a portal, so it moves
	 * with the mobile drawer and never covers the chat beside a docked sidebar.
	 */
	import { tick, type Snippet } from 'svelte';

	interface Props {
		open: boolean;
		/** Accessible name of the sheet (dialog or menu). */
		label: string;
		role?: 'dialog' | 'menu';
		/** True on the mobile shell: bottom sheet with a drag handle. */
		phone: boolean;
		/** Desktop placement class: 'flyout' (beside the rail) or 'popover'. */
		placement?: 'flyout' | 'popover';
		onClose: () => void;
		children: Snippet;
	}

	let { open, label, role = 'dialog', phone, placement = 'popover', onClose, children }: Props = $props();

	const DISMISS_AT = 70;

	let sheetRef = $state<HTMLDivElement | null>(null);
	let dragY = $state(0);
	let dragging = $state(false);
	let start: { y: number; id: number } | null = null;
	let returnFocus: HTMLElement | null = null;

	// Focus moves into the sheet when it opens and back to the opener when it
	// closes, so keyboard users never land behind the scrim.
	$effect(() => {
		if (open) {
			returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
			void tick().then(() => {
				const first = sheetRef?.querySelector<HTMLElement>('button:not([disabled]), [tabindex]:not([tabindex="-1"])');
				(first ?? sheetRef)?.focus({ preventScroll: true });
			});
			return () => {
				const target = returnFocus;
				returnFocus = null;
				if (target?.isConnected) target.focus({ preventScroll: true });
			};
		}
	});

	function onPointerDown(e: PointerEvent) {
		if (!phone || !e.isPrimary || e.button !== 0) return;
		start = { y: e.clientY, id: e.pointerId };
	}

	function onPointerMove(e: PointerEvent) {
		if (!start || e.pointerId !== start.id) return;
		const dy = e.clientY - start.y;
		if (!dragging) {
			if (Math.abs(dy) < 8) return;
			// Only a downward pull drags the sheet; anything else is a tap or a
			// scroll of its content.
			if (dy < 0) { start = null; return; }
			dragging = true;
			sheetRef?.setPointerCapture?.(e.pointerId);
		}
		e.preventDefault();
		dragY = Math.max(0, dy);
	}

	function onPointerEnd(e: PointerEvent) {
		if (!start || e.pointerId !== start.id) return;
		start = null;
		if (!dragging) return;
		dragging = false;
		const dismiss = dragY > DISMISS_AT;
		dragY = 0;
		if (dismiss) onClose();
		// The pointerup that ends a drag must not also click a pill under it.
		const swallow = (ev: Event) => { ev.stopPropagation(); ev.preventDefault(); };
		sheetRef?.addEventListener('click', swallow, { capture: true, once: true });
		setTimeout(() => sheetRef?.removeEventListener('click', swallow, { capture: true }), 0);
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
<div class="sheet-scrim" class:open class:phone class:clear={!phone} onclick={onClose}></div>
<div
	bind:this={sheetRef}
	class="sheet {phone ? 'phone' : placement}"
	class:open
	class:dragging
	{role}
	aria-label={label}
	aria-hidden={open ? undefined : 'true'}
	inert={!open}
	tabindex="-1"
	style:--sheet-drag="{dragY}px"
	onpointerdown={onPointerDown}
	onpointermove={onPointerMove}
	onpointerup={onPointerEnd}
	onpointercancel={onPointerEnd}
>
	{#if phone}<div class="handle" aria-hidden="true"></div>{/if}
	{@render children()}
</div>

<style>
	.sheet-scrim {
		position: absolute;
		inset: 0;
		z-index: 20;
		background: oklch(0 0 0 / 0.5);
		opacity: 0;
		pointer-events: none;
		transition: opacity 0.26s cubic-bezier(0.16, 1, 0.3, 1);
	}
	.sheet-scrim.clear { background: transparent; }
	.sheet-scrim.open { opacity: 1; pointer-events: auto; }

	.sheet {
		position: absolute;
		z-index: 21;
		background: var(--popover);
		color: var(--popover-foreground, var(--foreground));
		border: 1px solid var(--border);
		box-shadow: inset 0 1px 0 oklch(1 0 0 / 0.07), var(--shadow-l);
		outline: none;
	}

	/* Phone: a bottom sheet the thumb can pull down. */
	.sheet.phone {
		left: 6px;
		right: 6px;
		bottom: 6px;
		max-height: calc(100% - 80px);
		overflow-y: auto;
		padding: 6px 12px calc(24px + env(safe-area-inset-bottom, 0px));
		border-radius: 20px;
		touch-action: none;
		transform: translateY(calc(100% + 12px));
		transition: transform 0.32s cubic-bezier(0.16, 1, 0.3, 1);
	}
	.sheet.phone.open { transform: translateY(var(--sheet-drag, 0px)); }
	.sheet.phone.dragging { transition: none; }
	.handle {
		width: 38px;
		height: 5px;
		border-radius: 3px;
		background: var(--border-strong);
		margin: 4px auto 12px;
	}

	/* Desktop: a small popover that scales in from its anchor corner. */
	.sheet.flyout,
	.sheet.popover {
		padding: 6px;
		border-radius: 12px;
		opacity: 0;
		pointer-events: none;
		transition: transform 0.18s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.14s ease;
	}
	.sheet.flyout {
		left: 6px;
		bottom: 10px;
		width: 224px;
		transform-origin: 0 100%;
		transform: translateY(6px) scale(0.97);
	}
	.sheet.popover {
		right: 8px;
		top: 96px;
		width: min(280px, calc(100% - 16px));
		max-height: calc(100% - 110px);
		overflow-y: auto;
		padding: 10px 10px 4px;
		transform-origin: 100% 0;
		transform: translateY(-6px) scale(0.97);
	}
	.sheet.flyout.open,
	.sheet.popover.open { opacity: 1; pointer-events: auto; transform: none; }

	@media (prefers-reduced-motion: reduce) {
		.sheet, .sheet-scrim { transition: none; }
	}
</style>
