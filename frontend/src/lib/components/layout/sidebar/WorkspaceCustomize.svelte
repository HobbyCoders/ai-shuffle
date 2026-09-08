<script lang="ts">
	/**
	 * WorkspaceCustomize — the in-panel dialog behind the More flyout's
	 * "Customize" entry.
	 *
	 * Lists every Workspace destination the current role may open, each with a
	 * checkbox (shown inline vs folded into More) and a drag handle. "New
	 * session" is listed first and inert: it is not a Workspace destination and
	 * never moves, but leaving it out would make the dialog disagree with the
	 * panel it is describing.
	 *
	 * REORDERING has two paths on purpose. HTML5 drag covers the mouse, and the
	 * grip is a real button whose Up/Down arrows move the row one place — a
	 * pointer-only reorder would put the feature out of reach of a keyboard.
	 * Both call the same store action, and the keyed `{#each}` moves the
	 * existing DOM node, so the grip that was moved keeps focus and can be
	 * pressed again.
	 *
	 * The dialog draws over a dimmed veil inside the sidebar plane rather than
	 * in a portal: it is a preference for THIS panel, and staying inside it
	 * keeps the panel's own scroll and the mobile drawer intact.
	 */
	import { Check, GripVertical, SquarePen } from 'lucide-svelte';
	import {
		orderedWorkspaceItems,
		sidebarWorkspace,
		workspacePrefs,
		type WorkspaceItemId
	} from '$lib/stores/sidebar-prefs.svelte';
	import type { WorkspaceNavItem } from './workspace-items';

	interface Props {
		/** Destinations the current role may open, in canonical order. */
		items: WorkspaceNavItem[];
		onClose: () => void;
	}

	let { items, onClose }: Props = $props();

	const rows = $derived(orderedWorkspaceItems(items, $workspacePrefs));
	const shown = $derived(new Set($workspacePrefs.shown));

	let dragId = $state<WorkspaceItemId | null>(null);
	let overId = $state<WorkspaceItemId | null>(null);
	let dialogRef = $state<HTMLDivElement | null>(null);

	// Move focus into the dialog so Escape / Tab act on it, not on the row the
	// flyout was opened from (which is now behind the veil).
	$effect(() => {
		dialogRef?.focus();
	});

	function onDragStart(e: DragEvent, id: WorkspaceItemId) {
		dragId = id;
		if (e.dataTransfer) {
			e.dataTransfer.effectAllowed = 'move';
			// Firefox refuses to start a drag without payload.
			e.dataTransfer.setData('text/plain', id);
		}
	}

	function onDragOver(e: DragEvent, id: WorkspaceItemId) {
		if (!dragId || dragId === id) return;
		e.preventDefault();
		overId = id;
	}

	function onDrop(e: DragEvent, id: WorkspaceItemId) {
		if (!dragId) return;
		e.preventDefault();
		sidebarWorkspace.reorder(dragId, id);
		dragId = null;
		overId = null;
	}

	function onDragEnd() {
		dragId = null;
		overId = null;
	}

	function onGripKeydown(e: KeyboardEvent, id: WorkspaceItemId) {
		const delta = e.key === 'ArrowUp' ? -1 : e.key === 'ArrowDown' ? 1 : 0;
		if (!delta) return;
		e.preventDefault();
		sidebarWorkspace.move(id, delta);
	}
</script>

<div
	class="veil"
	role="presentation"
	onclick={onClose}
	onkeydown={(e) => { if (e.key === 'Escape') onClose(); }}
></div>

<div
	class="custom"
	role="dialog"
	aria-modal="true"
	aria-label="Customize workspace"
	tabindex="-1"
	bind:this={dialogRef}
>
	<div class="ph">
		<span class="ph-title">Customize workspace</span>
		<button class="lk" onclick={onClose}>Done</button>
	</div>
	<p class="hint">
		Checked rows show in the sidebar; the rest fold into More. Drag a handle, or move it with the
		arrow keys, to reorder.
	</p>

	<div class="crows">
		<div class="crow pinned">
			<span class="cb on" aria-hidden="true"><Check size={11} strokeWidth={2.5} /></span>
			<SquarePen size={15} strokeWidth={1.85} />
			<span class="cl">New session</span>
			<span class="lock">Always first</span>
		</div>

		{#each rows as item (item.id)}
			<div
				class="crow"
				class:dragging={dragId === item.id}
				class:over={overId === item.id}
				draggable="true"
				role="presentation"
				ondragstart={(e) => onDragStart(e, item.id)}
				ondragover={(e) => onDragOver(e, item.id)}
				ondrop={(e) => onDrop(e, item.id)}
				ondragend={onDragEnd}
			>
				<button
					class="cb"
					class:on={shown.has(item.id)}
					role="checkbox"
					aria-checked={shown.has(item.id)}
					aria-label={`${shown.has(item.id) ? 'Hide' : 'Show'} ${item.label}`}
					onclick={() => sidebarWorkspace.toggle(item.id)}
				>
					<Check size={11} strokeWidth={2.5} />
				</button>
				<item.icon size={15} strokeWidth={1.85} />
				<span class="cl">{item.label}</span>
				<button
					class="grip"
					aria-label={`Reorder ${item.label}`}
					title="Drag, or use the arrow keys, to reorder"
					onkeydown={(e) => onGripKeydown(e, item.id)}
				>
					<GripVertical size={14} strokeWidth={1.85} />
				</button>
			</div>
		{/each}
	</div>
</div>

<style>
	.veil {
		position: absolute;
		inset: 0;
		z-index: 130;
		background: color-mix(in oklch, var(--background) 55%, transparent);
		backdrop-filter: blur(2px);
		-webkit-backdrop-filter: blur(2px);
		animation: veil-in 180ms ease;
	}

	/* One popover recipe: lit top edge, blur, cast shadow, short spring in. */
	.custom {
		position: absolute;
		z-index: 131;
		left: 10px;
		right: 10px;
		top: 64px;
		max-height: calc(100% - 96px);
		overflow-y: auto;
		padding: 8px;
		border-radius: 12px;
		background: color-mix(in oklch, var(--popover) 92%, transparent);
		backdrop-filter: blur(16px) saturate(1.2);
		-webkit-backdrop-filter: blur(16px) saturate(1.2);
		border: 1px solid var(--border);
		box-shadow: inset 0 1px 0 oklch(1 0 0 / 0.08), var(--shadow-l);
		color: var(--popover-foreground);
		animation: pop-in 200ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	.ph {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 6px 4px 8px 8px;
	}
	.ph-title { font-size: 12.5px; font-weight: 600; }
	.lk {
		margin-left: auto;
		padding: 4px 6px;
		border: none;
		background: transparent;
		border-radius: 6px;
		color: var(--primary);
		font-size: 12px;
		font-weight: 600;
		cursor: pointer;
	}
	.lk:hover { background: color-mix(in oklch, var(--primary) 14%, transparent); }

	.hint {
		margin: 0;
		padding: 0 8px 8px;
		font-size: 11.5px;
		line-height: 1.4;
		color: var(--subtle-foreground);
	}

	.crows { display: flex; flex-direction: column; }
	.crow {
		display: flex;
		align-items: center;
		gap: 10px;
		height: 36px;
		padding: 0 4px 0 8px;
		border-radius: 8px;
		transition: background-color 0.13s ease, box-shadow 0.13s ease;
	}
	.crow:hover { background: var(--sidebar-accent); }
	.crow.dragging { opacity: 0.4; }
	.crow.over { box-shadow: inset 0 2px 0 var(--primary); }
	.crow.pinned { color: var(--muted-foreground); }
	.crow :global(svg) { flex: none; color: var(--subtle-foreground); }
	.cl {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 12.5px;
		color: var(--foreground);
	}
	.crow.pinned .cl { color: var(--muted-foreground); }
	.lock { font-size: 11px; color: var(--subtle-foreground); }

	.cb {
		width: 16px;
		height: 16px;
		flex: none;
		display: grid;
		place-items: center;
		padding: 0;
		border: 1px solid var(--border-strong);
		border-radius: 5px;
		background: var(--sidebar-accent);
		color: var(--primary-foreground);
		cursor: pointer;
		transition: background-color 0.13s ease, border-color 0.13s ease;
	}
	.cb :global(svg) { color: currentColor; opacity: 0; }
	.cb.on { background: var(--primary); border-color: var(--primary); }
	.cb.on :global(svg) { opacity: 1; }

	.grip {
		display: grid;
		place-items: center;
		padding: 5px 3px;
		border: none;
		background: transparent;
		border-radius: 5px;
		color: var(--subtle-foreground);
		cursor: grab;
	}
	.grip:hover { background: var(--sidebar-accent); color: var(--foreground); }

	@keyframes veil-in { from { opacity: 0; } to { opacity: 1; } }
	@keyframes pop-in {
		from { opacity: 0; transform: scale(0.96) translateY(-4px); }
		to { opacity: 1; transform: none; }
	}

	:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }

	/* Touch sizing: the mobile shell, plus any touch-only pointer. */
	@media (hover: none) {
		.crow { height: 44px; }
		.cb { width: 20px; height: 20px; }
		.grip { padding: 10px 6px; }
	}

	@media (prefers-reduced-motion: reduce) {
		.veil, .custom { animation: none; }
		.crow, .cb { transition: none; }
	}
</style>
