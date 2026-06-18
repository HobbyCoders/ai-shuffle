<script lang="ts">
	/**
	 * GroupedSelector - Reusable grouped dropdown menu with card-slide animation.
	 *
	 * Shows items organized by groups. Groups appear as parent rows;
	 * clicking one slides to a sub-panel with that group's items.
	 * Back button slides back. Works well on mobile and desktop.
	 */
	import { Check, ChevronRight, ChevronLeft, FolderClosed, FolderPlus } from 'lucide-svelte';
	import { groups, organizeByGroups, type EntityType } from '$lib/stores/groups.svelte';
	import { tick } from 'svelte';

	interface Item {
		id: string;
		name: string;
	}

	interface Props {
		items: Item[];
		entityType: EntityType;
		selectedId: string | null;
		onSelect: (id: string) => void;
		label?: string;
		addLabel?: string;
		onAdd?: () => void;
		emptyMessage?: string;
		/** Icon component for items (optional) */
		itemIcon?: any;
	}

	let {
		items,
		entityType,
		selectedId,
		onSelect,
		label = '',
		addLabel = '',
		onAdd,
		emptyMessage = 'No items available',
		itemIcon
	}: Props = $props();

	// Current view state
	let activeGroup = $state<string | null>(null);
	let slideDirection = $state<'forward' | 'back'>('forward');
	let isAnimating = $state(false);

	// Organize items by groups
	const organized = $derived.by(() => {
		const groupsState = $groups;
		return organizeByGroups(items, entityType, groupsState);
	});

	// Groups that actually have items
	const populatedGroups = $derived.by(() => {
		const result: { name: string; items: Item[]; collapsed: boolean }[] = [];
		for (const groupDef of organized.groupOrder) {
			const groupItems = organized.grouped.get(groupDef.name) || [];
			if (groupItems.length > 0) {
				result.push({ name: groupDef.name, items: groupItems, collapsed: groupDef.collapsed });
			}
		}
		return result;
	});

	// Has any groups at all
	const hasGroups = $derived(populatedGroups.length > 0);

	// Items in the active sub-panel
	const activeGroupItems = $derived.by(() => {
		if (!activeGroup) return [];
		return organized.grouped.get(activeGroup) || [];
	});

	// Panel height tracking for smooth resize
	let mainPanelEl = $state<HTMLDivElement | null>(null);
	let subPanelEl = $state<HTMLDivElement | null>(null);
	let containerHeight = $state<number | null>(null);

	function openGroup(groupName: string) {
		if (isAnimating) return;
		slideDirection = 'forward';
		isAnimating = true;
		// Capture current height
		if (mainPanelEl) containerHeight = mainPanelEl.scrollHeight;
		activeGroup = groupName;
		// After render, update height to sub-panel
		tick().then(() => {
			if (subPanelEl) containerHeight = subPanelEl.scrollHeight;
			setTimeout(() => { isAnimating = false; }, 280);
		});
	}

	function goBack() {
		if (isAnimating) return;
		slideDirection = 'back';
		isAnimating = true;
		// Capture current height
		if (subPanelEl) containerHeight = subPanelEl.scrollHeight;
		activeGroup = null;
		tick().then(() => {
			if (mainPanelEl) containerHeight = mainPanelEl.scrollHeight;
			setTimeout(() => {
				isAnimating = false;
				containerHeight = null; // Let it be auto again
			}, 280);
		});
	}

	function handleSelect(id: string) {
		onSelect(id);
		// Reset to main panel for next open
		activeGroup = null;
		containerHeight = null;
		isAnimating = false;
	}

	// Reset state when menu closes/opens (items change significantly)
	export function reset() {
		activeGroup = null;
		containerHeight = null;
		isAnimating = false;
	}
</script>

<div
	class="grouped-selector"
	style:height={containerHeight != null ? `${containerHeight}px` : 'auto'}
	class:animating={isAnimating}
>
	<!-- Main Panel -->
	<div
		class="panel main-panel"
		class:active={activeGroup === null}
		class:slide-out-left={activeGroup !== null}
		bind:this={mainPanelEl}
	>
		{#if label}
			<p class="panel-label">{label}</p>
		{/if}

		<div class="panel-scroll">
			{#if !hasGroups}
				<!-- No groups: show flat list -->
				{#each items as item (item.id)}
					<button
						type="button"
						class="menu-item"
						class:selected={selectedId === item.id}
						onclick={() => handleSelect(item.id)}
					>
						{#if itemIcon}
							{@const ItemIcon = itemIcon}
							<ItemIcon size={16} strokeWidth={1.5} />
						{/if}
						<span class="item-name">{item.name}</span>
						{#if selectedId === item.id}
							<Check size={16} strokeWidth={2} class="check-icon" />
						{/if}
					</button>
				{:else}
					<p class="empty-message">{emptyMessage}</p>
				{/each}
			{:else}
				<!-- Grouped: show group headers + ungrouped items -->
				{#each populatedGroups as group (group.name)}
					<button
						type="button"
						class="menu-item group-item"
						onclick={() => openGroup(group.name)}
					>
						<FolderClosed size={16} strokeWidth={1.5} />
						<span class="item-name">{group.name}</span>
						<span class="group-count">{group.items.length}</span>
						<ChevronRight size={16} strokeWidth={1.5} class="chevron-right" />
					</button>
				{/each}

				{#if organized.ungrouped.length > 0}
					{#if populatedGroups.length > 0}
						<div class="menu-divider"></div>
					{/if}
					{#each organized.ungrouped as item (item.id)}
						<button
							type="button"
							class="menu-item"
							class:selected={selectedId === item.id}
							onclick={() => handleSelect(item.id)}
						>
							{#if itemIcon}
								{@const ItemIcon = itemIcon}
								<ItemIcon size={16} strokeWidth={1.5} />
							{/if}
							<span class="item-name">{item.name}</span>
							{#if selectedId === item.id}
								<Check size={16} strokeWidth={2} class="check-icon" />
							{/if}
						</button>
					{/each}
				{/if}
			{/if}
		</div>

		{#if addLabel && onAdd}
			<div class="menu-divider"></div>
			<button type="button" class="menu-item add-item" onclick={onAdd}>
				<FolderPlus size={16} strokeWidth={1.5} />
				<span class="item-name">{addLabel}</span>
			</button>
		{/if}
	</div>

	<!-- Sub Panel (group items) -->
	<div
		class="panel sub-panel"
		class:active={activeGroup !== null}
		class:slide-in-right={activeGroup !== null}
		class:offscreen-right={activeGroup === null}
		bind:this={subPanelEl}
	>
		<button type="button" class="back-button" onclick={goBack}>
			<ChevronLeft size={16} strokeWidth={1.5} />
			<span class="back-label">{activeGroup ?? ''}</span>
		</button>

		<div class="menu-divider"></div>

		<div class="panel-scroll">
			{#each activeGroupItems as item (item.id)}
				<button
					type="button"
					class="menu-item"
					class:selected={selectedId === item.id}
					onclick={() => handleSelect(item.id)}
				>
					{#if itemIcon}
						{@const ItemIcon = itemIcon}
						<ItemIcon size={16} strokeWidth={1.5} />
					{/if}
					<span class="item-name">{item.name}</span>
					{#if selectedId === item.id}
						<Check size={16} strokeWidth={2} class="check-icon" />
					{/if}
				</button>
			{:else}
				<p class="empty-message">No items in this group</p>
			{/each}
		</div>
	</div>
</div>

<style>
	.grouped-selector {
		position: relative;
		overflow: hidden;
		transition: height 250ms cubic-bezier(0.32, 0.72, 0, 1);
	}

	/* Panels */
	.panel {
		width: 100%;
	}

	.main-panel {
		transition: transform 250ms cubic-bezier(0.32, 0.72, 0, 1), opacity 250ms ease;
		transform: translateX(0);
		opacity: 1;
	}

	.main-panel.slide-out-left {
		position: absolute;
		top: 0;
		left: 0;
		transform: translateX(-100%);
		opacity: 0;
		pointer-events: none;
	}

	.sub-panel {
		transition: transform 250ms cubic-bezier(0.32, 0.72, 0, 1), opacity 250ms ease;
		transform: translateX(0);
		opacity: 1;
	}

	.sub-panel.offscreen-right {
		position: absolute;
		top: 0;
		left: 0;
		transform: translateX(100%);
		opacity: 0;
		pointer-events: none;
	}

	/* Labels */
	.panel-label {
		padding: 8px 12px 4px;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		font-weight: 500;
	}

	/* Scroll area */
	.panel-scroll {
		max-height: min(40vh, 320px);
		overflow-y: auto;
		overscroll-behavior: contain;
	}

	.panel-scroll::-webkit-scrollbar {
		width: 6px;
	}

	.panel-scroll::-webkit-scrollbar-track {
		background: transparent;
	}

	.panel-scroll::-webkit-scrollbar-thumb {
		background: var(--scrollbar-thumb);
		border-radius: calc(var(--radius, 0.75rem) * 0.25);
	}

	.panel-scroll::-webkit-scrollbar-thumb:hover {
		background: var(--scrollbar-thumb-hover);
	}

	/* Menu items */
	.menu-item {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 10px 12px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		color: var(--foreground);
		font-size: 0.875rem;
		cursor: pointer;
		text-align: left;
		transition: background-color 120ms ease;
	}

	.menu-item:hover {
		background: var(--hover-overlay);
	}

	.menu-item.selected {
		background: color-mix(in oklch, var(--primary) 8%, transparent);
	}

	.menu-item :global(svg) {
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.menu-item.selected :global(svg) {
		color: var(--foreground);
	}

	.item-name {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.menu-item :global(.check-icon) {
		color: var(--primary);
	}

	/* Group item */
	.group-item {
		font-weight: 500;
	}

	.group-count {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		background: var(--muted);
		border-radius: calc(var(--radius, 0.75rem) * 0.83);
		padding: 1px 8px;
		min-width: 22px;
		text-align: center;
	}

	.group-item :global(.chevron-right) {
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	/* Back button */
	.back-button {
		display: flex;
		align-items: center;
		gap: 6px;
		width: 100%;
		padding: 8px 12px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		color: var(--muted-foreground);
		font-size: 0.8125rem;
		font-weight: 500;
		cursor: pointer;
		text-align: left;
		transition: background-color 120ms ease;
	}

	.back-button:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.back-button :global(svg) {
		flex-shrink: 0;
	}

	.back-label {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* Divider */
	.menu-divider {
		height: 1px;
		background: var(--border);
		margin: 4px 0;
	}

	/* Add item */
	.add-item {
		color: var(--primary) !important;
	}

	.add-item :global(svg) {
		color: var(--primary) !important;
	}

	/* Empty state */
	.empty-message {
		padding: 12px;
		text-align: center;
		color: var(--muted-foreground);
		font-size: 0.8125rem;
	}

	/* Reduced motion */
	@media (prefers-reduced-motion: reduce) {
		.grouped-selector,
		.main-panel,
		.sub-panel {
			transition: none;
		}
	}
</style>
