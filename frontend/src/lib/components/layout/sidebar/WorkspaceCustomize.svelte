<script lang="ts">
	/**
	 * WorkspaceCustomize — "Customize the rail".
	 *
	 * Lists every destination the current role may open, each with a checkbox
	 * (on the rail, or waiting under More) and a drag handle. New and Settings
	 * are listed as pinned rows: they never move, but leaving them out would
	 * make the dialog disagree with the rail it describes.
	 *
	 * On the phone it is a full page that slides in over the panel, with a
	 * Back button. On desktop it is a dialog over a dimmed veil inside the
	 * sidebar plane, so it never covers the chat.
	 *
	 * REORDERING has two paths. Dragging a handle (mouse or touch) moves the
	 * row live by comparing the pointer with the other rows' midpoints and
	 * commits once on release. The handle is also a real button: Up/Down move
	 * the row one place among the visible rows, so the feature is in reach of
	 * a keyboard. The keyed `{#each}` moves the existing DOM node, so the
	 * handle that was moved keeps focus.
	 */
	import { tick } from 'svelte';
	import { ArrowLeft, Check, GripVertical, Settings, SquarePen } from 'lucide-svelte';
	import {
		orderedWorkspaceItems,
		sidebarWorkspace,
		workspacePrefs,
		type WorkspaceItemId
	} from '$lib/stores/sidebar-prefs.svelte';
	import type { WorkspaceNavItem } from './workspace-items';

	interface Props {
		/** Destinations the current role may open (Settings excluded). */
		items: WorkspaceNavItem[];
		/** Show the pinned Settings row (admins). */
		showSettings?: boolean;
		phone: boolean;
		onClose: () => void;
	}

	let { items, showSettings = false, phone, onClose }: Props = $props();

	const ordered = $derived(orderedWorkspaceItems(items, $workspacePrefs));
	const shown = $derived(new Set($workspacePrefs.shown));
	const onRail = $derived(ordered.filter((item) => shown.has(item.id)).length);

	/** While a handle is held: the row order being previewed. */
	let draft = $state<WorkspaceItemId[] | null>(null);
	let liftId = $state<WorkspaceItemId | null>(null);
	const rows = $derived.by(() => {
		if (!draft) return ordered;
		const byId = new Map(ordered.map((item) => [item.id, item]));
		return draft.map((id) => byId.get(id)).filter((item): item is WorkspaceNavItem => !!item);
	});

	let listRef = $state<HTMLDivElement | null>(null);
	let doneRef = $state<HTMLButtonElement | null>(null);

	// Focus moves into the dialog so Tab and Escape act on it, not on the
	// rail behind the veil.
	$effect(() => {
		void tick().then(() => doneRef?.focus({ preventScroll: true }));
	});

	function onGripPointerDown(e: PointerEvent, id: WorkspaceItemId) {
		if (!e.isPrimary || e.button !== 0) return;
		e.preventDefault();
		draft = ordered.map((item) => item.id);
		liftId = id;
		let moved = false;
		const pointerId = e.pointerId;

		const move = (ev: PointerEvent) => {
			if (ev.pointerId !== pointerId || !draft || !listRef) return;
			ev.preventDefault();
			const others = [...listRef.querySelectorAll<HTMLElement>('[data-cid]')].filter((el) => el.dataset.cid !== id);
			const next = others.find((el) => {
				const r = el.getBoundingClientRect();
				return ev.clientY < r.top + r.height / 2;
			});
			const without = draft.filter((x) => x !== id);
			const at = next ? without.indexOf(next.dataset.cid as WorkspaceItemId) : without.length;
			without.splice(at, 0, id);
			if (without.some((x, i) => x !== draft![i])) {
				draft = without;
				moved = true;
				navigator.vibrate?.(8);
			}
		};
		const end = (ev: PointerEvent) => {
			if (ev.pointerId !== pointerId) return;
			window.removeEventListener('pointermove', move);
			window.removeEventListener('pointerup', end);
			window.removeEventListener('pointercancel', end);
			if (moved && draft && ev.type === 'pointerup') sidebarWorkspace.arrange(draft);
			draft = null;
			liftId = null;
		};
		window.addEventListener('pointermove', move, { passive: false });
		window.addEventListener('pointerup', end);
		window.addEventListener('pointercancel', end);
	}

	function onGripKeydown(e: KeyboardEvent, id: WorkspaceItemId) {
		const delta = e.key === 'ArrowUp' ? -1 : e.key === 'ArrowDown' ? 1 : 0;
		if (!delta) return;
		e.preventDefault();
		const ids = ordered.map((item) => item.id);
		const from = ids.indexOf(id);
		const to = from + delta;
		if (from < 0 || to < 0 || to >= ids.length) return;
		ids.splice(to, 0, ...ids.splice(from, 1));
		sidebarWorkspace.arrange(ids);
	}
</script>

{#if !phone}
	<!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
	<div class="cust-veil" onclick={onClose}></div>
{/if}

<div class="cust" class:phone role="dialog" aria-modal="true" aria-label="Customize the rail">
	<div class="cust-head">
		{#if phone}
			<button class="back" type="button" aria-label="Back" onclick={onClose}>
				<ArrowLeft size={20} strokeWidth={1.85} />
			</button>
		{/if}
		<b>Customize the rail</b>
		<button class="done" type="button" bind:this={doneRef} onclick={onClose}>Done</button>
	</div>
	<p class="cust-hint">Checked items sit on the rail. The rest wait under More. Drag a handle to reorder.</p>

	<div class="cust-list" class:reordering={!!liftId}>
		<div class="cust-label">On the rail <span>{onRail} of {ordered.length}</span></div>
		<div class="crow2 pinned">
			<span class="ckb on" aria-hidden="true"><Check size={phone ? 14 : 11} strokeWidth={3} /></span>
			<SquarePen size={phone ? 18 : 15} strokeWidth={1.85} />
			<span class="cl">New</span>
			<span class="lock">Always first</span>
		</div>

		<div bind:this={listRef}>
			{#each rows as item (item.id)}
				{@const on = shown.has(item.id)}
				<div class="crow2" class:off={!on} class:lift={liftId === item.id} data-cid={item.id}>
					<button
						class="ckb"
						class:on
						type="button"
						role="checkbox"
						aria-checked={on}
						aria-label={on ? `Move ${item.label} to More` : `Show ${item.label} on the rail`}
						onclick={() => sidebarWorkspace.toggle(item.id)}
					>
						<Check size={phone ? 14 : 11} strokeWidth={3} />
					</button>
					<item.icon size={phone ? 18 : 15} strokeWidth={1.85} />
					<span class="cl">{item.label}</span>
					<button
						class="grip"
						type="button"
						aria-label={`Reorder ${item.label}`}
						title="Drag, or use the arrow keys, to reorder"
						onpointerdown={(e) => onGripPointerDown(e, item.id)}
						onkeydown={(e) => onGripKeydown(e, item.id)}
					>
						<GripVertical size={phone ? 18 : 14} strokeWidth={1.85} />
					</button>
				</div>
			{/each}
		</div>

		{#if showSettings}
			<div class="crow2 pinned">
				<span class="ckb on" aria-hidden="true"><Check size={phone ? 14 : 11} strokeWidth={3} /></span>
				<Settings size={phone ? 18 : 15} strokeWidth={1.85} />
				<span class="cl">Settings</span>
				<span class="lock">Always last</span>
			</div>
		{/if}
	</div>
</div>

<style>
	.cust-veil {
		position: absolute;
		inset: 0;
		z-index: 30;
		background: oklch(0 0 0 / 0.45);
		animation: veil-in 180ms ease;
	}

	.cust {
		position: absolute;
		z-index: 31;
		display: flex;
		flex-direction: column;
		left: 66px;
		right: 10px;
		top: 12px;
		max-height: calc(100% - 24px);
		border-radius: 14px;
		background: var(--popover);
		color: var(--popover-foreground, var(--foreground));
		border: 1px solid var(--border);
		box-shadow: inset 0 1px 0 oklch(1 0 0 / 0.07), var(--shadow-l);
		animation: pop-in 200ms cubic-bezier(0.16, 1, 0.3, 1);
	}
	/* Phone: a full page that slides in from the right. */
	.cust.phone {
		inset: 0;
		max-height: none;
		border: 0;
		border-radius: 0;
		box-shadow: none;
		background: var(--sidebar);
		padding-top: env(safe-area-inset-top, 0px);
		animation: cust-in 300ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	.cust-head { display: flex; align-items: center; gap: 6px; padding: 8px 8px 4px; }
	.cust-head b {
		padding-left: 6px;
		font-size: 13px;
		font-weight: 600;
		letter-spacing: -0.01em;
		color: var(--foreground);
	}
	.phone .cust-head b { padding-left: 0; font-size: 15px; }
	.back {
		width: 44px;
		height: 44px;
		display: grid;
		place-items: center;
		padding: 0;
		border: 0;
		border-radius: 10px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
	}
	.back:hover { background: var(--sidebar-accent); color: var(--foreground); }
	.done {
		margin-left: auto;
		padding: 5px 8px;
		border: 0;
		border-radius: 8px;
		background: transparent;
		color: var(--primary);
		font: inherit;
		font-size: 12.5px;
		font-weight: 600;
		cursor: pointer;
	}
	.phone .done { padding: 8px 10px; font-size: 14px; }
	.done:hover { background: color-mix(in oklch, var(--primary) 14%, transparent); }

	.cust-hint {
		margin: 0;
		padding: 0 14px 8px;
		font-size: 11.5px;
		line-height: 1.45;
		color: var(--muted-foreground);
	}
	.phone .cust-hint { padding: 2px 16px 12px; font-size: 13px; }

	.cust-list { flex: 1; min-height: 0; overflow-y: auto; padding: 0 6px 8px; }
	.phone .cust-list { padding: 0 8px calc(20px + env(safe-area-inset-bottom, 0px)); }
	.cust-label {
		display: flex;
		align-items: baseline;
		gap: 6px;
		padding: 10px 10px 4px;
		font-size: 12px;
		font-weight: 600;
		color: var(--muted-foreground);
	}
	.cust-label span { font-weight: 450; color: var(--subtle-foreground); }

	.crow2 {
		position: relative;
		display: flex;
		align-items: center;
		gap: 10px;
		height: 36px;
		padding: 0 4px 0 10px;
		border-radius: 8px;
		transition: background-color 120ms ease, box-shadow 160ms ease, transform 160ms cubic-bezier(0.16, 1, 0.3, 1);
	}
	.phone .crow2 { height: 52px; gap: 12px; border-radius: 12px; background: var(--sidebar); }
	.crow2:not(.pinned):hover { background: var(--sidebar-accent); }
	.crow2 > :global(svg) { flex: none; color: var(--muted-foreground); }
	.cl {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 12.5px;
		color: var(--foreground);
	}
	.phone .cl { font-size: 15px; }
	.crow2.off .cl { color: var(--muted-foreground); }
	.crow2.pinned .cl { color: var(--muted-foreground); }
	.lock { padding-right: 10px; font-size: 11px; color: var(--subtle-foreground); }
	.phone .lock { font-size: 12px; }
	.crow2.lift {
		z-index: 2;
		background: var(--popover);
		box-shadow: 0 10px 28px -8px oklch(0 0 0 / 0.8), inset 0 0 0 1px var(--border);
		transform: scale(1.02);
	}

	.ckb {
		width: 16px;
		height: 16px;
		flex: none;
		display: grid;
		place-items: center;
		padding: 0;
		border: 1px solid var(--border-strong);
		border-radius: 5px;
		background: transparent;
		color: var(--primary-foreground);
		cursor: pointer;
		transition: background-color 0.13s ease, border-color 0.13s ease;
	}
	.phone .ckb { width: 22px; height: 22px; border-width: 1.5px; border-radius: 7px; }
	.ckb :global(svg) { opacity: 0; }
	.ckb.on { background: var(--primary); border-color: var(--primary); }
	.ckb.on :global(svg) { opacity: 1; }
	.crow2.pinned .ckb { opacity: 0.45; cursor: default; }

	.grip {
		width: 26px;
		height: 28px;
		flex: none;
		display: grid;
		place-items: center;
		padding: 0;
		border: 0;
		border-radius: 6px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: grab;
		touch-action: none;
	}
	.phone .grip { width: 44px; height: 44px; border-radius: 10px; }
	.grip:hover { background: var(--sidebar-accent-active); color: var(--foreground); }
	.reordering .grip { cursor: grabbing; }

	:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }

	@media (hover: none) {
		.crow2 { height: 44px; }
		.ckb { width: 20px; height: 20px; }
		.grip { width: 40px; height: 40px; }
	}

	@keyframes veil-in { from { opacity: 0; } to { opacity: 1; } }
	@keyframes cust-in { from { transform: translateX(100%); } to { transform: none; } }
	@keyframes pop-in {
		from { opacity: 0; transform: scale(0.96) translateY(-4px); }
		to { opacity: 1; transform: none; }
	}

	@media (prefers-reduced-motion: reduce) {
		.cust-veil, .cust { animation: none; }
		.crow2, .ckb { transition: none; }
	}
</style>
