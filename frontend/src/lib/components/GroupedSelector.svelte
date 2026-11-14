<script lang="ts">
	/**
	 * GroupedSelector - Reusable grouped dropdown menu with card-slide animation.
	 *
	 * Shows items organized by groups. Groups appear as parent rows;
	 * clicking one slides to a sub-panel with that group's items.
	 * Back button slides back. Works well on mobile and desktop.
	 *
	 * `searchable` adds a search field over the main panel and lists the items
	 * flat, ignoring groups, in the resting order `rank` gives (a quick picker:
	 * most recently used first). Typing lists the matches best first, ↑/↓ move
	 * the highlight, Enter
	 * picks it, and with `numbered` a digit picks one of the first nine rows
	 * while the field is empty. Escape is left to the menu's owner.
	 */
	import { Check, ChevronRight, ChevronLeft, FolderClosed, FolderPlus, Search } from 'lucide-svelte';
	import { groups, organizeByGroups, type EntityType } from '$lib/stores/groups.svelte';
	import { tick } from 'svelte';

	interface Item {
		id: string;
		name: string;
		description?: string | null;
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
		/** Optional groups rendered after the primary items. */
		extraGroups?: { name: string; items: Item[] }[];
		/** One line of each item's description under its name (a full-page list). */
		showDescriptions?: boolean;
		/** A search field over the main panel (see the header). */
		searchable?: boolean;
		searchLabel?: string;
		/**
		 * The items to show for a search, in order; `''` gives the list's resting
		 * order. Default: the given order, filtered by a case-insensitive substring.
		 */
		rank?: (items: Item[], query: string) => Item[];
		/** Number the first nine item rows; a digit with an empty search picks one. */
		numbered?: boolean;
		/** A row after the items that is not one of them ("No project"); 0 picks it. */
		trailingItem?: Item | null;
		trailingIcon?: any;
		/** Focus the search field when the menu mounts. */
		autofocus?: boolean;
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
		itemIcon,
		extraGroups = [],
		showDescriptions = false,
		searchable = false,
		searchLabel = 'Search',
		rank,
		numbered = false,
		trailingItem = null,
		trailingIcon,
		autofocus = false
	}: Props = $props();

	const uid = $props.id();
	let query = $state('');
	let highlight = $state(0);
	let searchInput = $state<HTMLInputElement | null>(null);

	function substringRank(list: Item[], q: string): Item[] {
		const needle = q.trim().toLowerCase();
		return needle ? list.filter(item => item.name.toLowerCase().includes(needle)) : list;
	}
	const rankItems = $derived(rank ?? substringRank);
	/** The resting order (e.g. most recently used first). */
	const ordered = $derived(rankItems(items, ''));
	const searching = $derived(searchable && query.trim() !== '');
	const matches = $derived(searching ? rankItems(items, query) : []);
	const showTrailing = $derived(
		!!trailingItem && (!searching || trailingItem.name.toLowerCase().includes(query.trim().toLowerCase()))
	);

	/** With a description showing, a row is still called by its name alone. */
	const itemNaming = (item: Item): Record<string, string | undefined> =>
		showDescriptions && item.description?.trim()
			? { 'aria-label': item.name, 'aria-description': item.description.trim() }
			: {};

	// Current view state
	let activeGroup = $state<string | null>(null);
	let slideDirection = $state<'forward' | 'back'>('forward');
	let isAnimating = $state(false);

	// Organize items by groups
	const organized = $derived.by(() => {
		const groupsState = $groups;
		return organizeByGroups(ordered, entityType, groupsState);
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
	/** Group rows on the main panel. A searchable list stays flat, so its
	 *  resting order and 1–9 keys cover every item, grouped or not. */
	const grouped = $derived(hasGroups && !searchable);

	const extraPopulated = $derived(extraGroups.filter(g => g.items.length > 0));

	const extraGroupMap = $derived.by(() => {
		const map = new Map<string, Item[]>();
		for (const group of extraGroups) map.set(group.name, group.items);
		return map;
	});

	// Items in the active sub-panel
	const activeGroupItems = $derived.by(() => {
		if (!activeGroup) return [];
		return extraGroupMap.get(activeGroup) || organized.grouped.get(activeGroup) || [];
	});

	/** The main panel's rows in screen order, for the highlight and digit keys. */
	type Row = { kind: 'item' | 'group' | 'trailing'; id: string };
	const rows = $derived.by((): Row[] => {
		if (!searchable) return [];
		const list: Row[] = [];
		if (searching) {
			for (const item of matches) list.push({ kind: 'item', id: item.id });
		} else {
			if (grouped) for (const group of populatedGroups) list.push({ kind: 'group', id: group.name });
			for (const item of grouped ? organized.ungrouped : ordered) list.push({ kind: 'item', id: item.id });
			for (const group of extraPopulated) list.push({ kind: 'group', id: group.name });
		}
		if (showTrailing && trailingItem) list.push({ kind: 'trailing', id: trailingItem.id });
		return list;
	});
	const rowIndex = $derived(new Map(rows.map((row, index) => [`${row.kind}:${row.id}`, index])));
	const active = $derived(rows.length ? Math.min(highlight, rows.length - 1) : -1);
	/** Item ids with their 1–9 key (only while the search is empty: then a digit types). */
	const digitOf = $derived(
		new Map(numbered && !query ? rows.filter(row => row.kind === 'item').slice(0, 9).map((row, i) => [row.id, i + 1]) : [])
	);
	const rowId = (index: number | undefined) => (index === undefined ? undefined : `${uid}-row-${index}`);
	/** Attributes a main-panel row carries while the menu is searchable. */
	function rowProps(kind: Row['kind'], id: string): Record<string, unknown> {
		if (!searchable) return {};
		const index = rowIndex.get(`${kind}:${id}`);
		return {
			id: rowId(index),
			'data-highlighted': index === active ? '' : undefined,
			onpointermove: () => { if (index !== undefined) highlight = index; }
		};
	}

	function activate(row: Row) {
		if (row.kind === 'group') openGroup(row.id);
		else handleSelect(row.id);
	}

	function searchKeydown(event: KeyboardEvent) {
		if (event.isComposing) return;
		if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
			event.preventDefault();
			if (!rows.length) return;
			highlight = (active + (event.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
			tick().then(() => document.getElementById(`${uid}-row-${highlight}`)?.scrollIntoView?.({ block: 'nearest' }));
		} else if (event.key === 'Enter') {
			event.preventDefault();
			const row = rows[active];
			if (row) activate(row);
		} else if (numbered && !query && /^[0-9]$/.test(event.key) && !event.ctrlKey && !event.metaKey && !event.altKey) {
			// No row for that digit: it types, as in any search.
			const row = event.key === '0'
				? rows.find(r => r.kind === 'trailing')
				: rows.find(r => r.kind === 'item' && digitOf.get(r.id) === Number(event.key));
			if (row) {
				event.preventDefault();
				activate(row);
			}
		}
	}

	function focusOnMount(node: HTMLInputElement) {
		// After the owner's menu actions: a menu floated to <body> moves this
		// node, and moving a focused node drops its focus.
		if (autofocus) queueMicrotask(() => { if (node.isConnected) node.focus({ preventScroll: true }); });
	}

	/** Put the cursor in the search field (the menu was opened from the keyboard). */
	export function focusSearch() {
		searchInput?.focus({ preventScroll: true });
	}

	// Panel height tracking for smooth resize
	let mainPanelEl = $state<HTMLDivElement | null>(null);
	let subPanelEl = $state<HTMLDivElement | null>(null);
	let containerHeight = $state<number | null>(null);
	/** The group row that opened the sub-panel by keyboard, focused again on Back. */
	let opener: HTMLElement | null = null;

	function openGroup(groupName: string) {
		if (isAnimating) return;
		slideDirection = 'forward';
		isAnimating = true;
		// Capture current height
		if (mainPanelEl) containerHeight = mainPanelEl.scrollHeight;
		const active = document.activeElement;
		const hadFocus = active instanceof HTMLElement && (mainPanelEl?.contains(active) ?? false);
		opener = hadFocus ? (active as HTMLElement) : null;
		activeGroup = groupName;
		// After render, update height to sub-panel
		tick().then(() => {
			if (subPanelEl) containerHeight = subPanelEl.scrollHeight;
			if (hadFocus) focusFirst(subPanelEl);
			setTimeout(() => { isAnimating = false; }, 280);
		});
	}

	function goBack() {
		if (isAnimating) return;
		slideDirection = 'back';
		isAnimating = true;
		// Capture current height
		if (subPanelEl) containerHeight = subPanelEl.scrollHeight;
		const hadFocus = subPanelEl?.contains(document.activeElement) ?? false;
		activeGroup = null;
		tick().then(() => {
			if (mainPanelEl) containerHeight = mainPanelEl.scrollHeight;
			// Back to the group row that opened it: the list kept its scroll, so
			// that row is still in view where the first row may not be.
			if (hadFocus) (opener?.isConnected ? opener : mainPanelEl?.querySelector<HTMLElement>('button'))?.focus({ preventScroll: true });
			opener = null;
			setTimeout(() => {
				isAnimating = false;
				containerHeight = null; // Let it be auto again
			}, 280);
		});
	}

	/** The hidden panel is inert, so keyboard focus follows the slide. No
	 *  scroll: the panel is still sliding in, and scrolling it into view would
	 *  push the clipped track sideways. */
	function focusFirst(panel: HTMLElement | null) {
		panel?.querySelector<HTMLElement>('button')?.focus({ preventScroll: true });
	}

	function handleSelect(id: string) {
		onSelect(id);
		// Reset to main panel for next open
		activeGroup = null;
		containerHeight = null;
		isAnimating = false;
		opener = null;
	}

	// Reset state when menu closes/opens (items change significantly)
	export function reset() {
		query = '';
		highlight = 0;
		activeGroup = null;
		containerHeight = null;
		isAnimating = false;
		opener = null;
	}
</script>

{#snippet rowKey(id: string)}
	{#if digitOf.has(id)}<kbd class="row-key" aria-hidden="true">{digitOf.get(id)}</kbd>{/if}
{/snippet}

{#snippet itemButton(item: Item)}
	<button
		type="button"
		class="menu-item"
		class:selected={selectedId === item.id}
		{...itemNaming(item)}
		{...rowProps('item', item.id)}
		onclick={() => handleSelect(item.id)}
	>
		{#if itemIcon}
			{@const ItemIcon = itemIcon}
			<ItemIcon size={16} strokeWidth={1.5} />
		{/if}
		{@render itemLabel(item)}
		{#if selectedId === item.id}
			<Check size={16} strokeWidth={2} class="check-icon" />
		{/if}
		{@render rowKey(item.id)}
	</button>
{/snippet}

{#snippet itemLabel(item: Item)}
	{#if showDescriptions && item.description?.trim()}
		<span class="item-copy"><span class="item-name">{item.name}</span><span class="item-desc">{item.description.trim()}</span></span>
	{:else}
		<span class="item-name">{item.name}</span>
	{/if}
{/snippet}

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
		inert={activeGroup !== null}
		bind:this={mainPanelEl}
	>
		{#if label}
			<p class="panel-label">{label}</p>
		{/if}

		{#if searchable}
			<div class="search-row">
				<Search size={14} strokeWidth={1.75} aria-hidden="true" />
				<input
					bind:this={searchInput}
					bind:value={query}
					use:focusOnMount
					type="text"
					class="search-input"
					placeholder={searchLabel}
					aria-label={searchLabel}
					aria-controls="{uid}-rows"
					aria-activedescendant={rowId(active >= 0 ? active : undefined)}
					autocomplete="off"
					spellcheck="false"
					enterkeyhint="go"
					oninput={() => (highlight = 0)}
					onkeydown={searchKeydown}
				/>
			</div>
		{/if}

		<div class="panel-scroll" id="{uid}-rows">
			{#if searching}
				{#each matches as item (item.id)}
					{@render itemButton(item)}
				{:else}
					{#if !showTrailing}
						<p class="empty-message">No matches</p>
					{/if}
				{/each}
			{:else if !grouped}
				<!-- No groups (or searchable): show flat list -->
				{#each ordered as item (item.id)}
					{@render itemButton(item)}
				{:else}
					{#if extraPopulated.length === 0}
						<p class="empty-message">{emptyMessage}</p>
					{/if}
				{/each}
			{:else}
				<!-- Grouped: show group headers + ungrouped items -->
				{#each populatedGroups as group (group.name)}
					<button
						type="button"
						class="menu-item group-item"
						{...rowProps('group', group.name)}
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
						{@render itemButton(item)}
					{/each}
				{/if}
			{/if}

			{#if extraPopulated.length > 0 && !searching}
				{#if items.length > 0}
					<div class="menu-divider"></div>
				{/if}
				{#each extraPopulated as group (group.name)}
					<button
						type="button"
						class="menu-item group-item"
						{...rowProps('group', group.name)}
						onclick={() => openGroup(group.name)}
					>
						<FolderClosed size={16} strokeWidth={1.5} />
						<span class="item-name">{group.name}</span>
						<span class="group-count">{group.items.length}</span>
						<ChevronRight size={16} strokeWidth={1.5} class="chevron-right" />
					</button>
				{/each}
			{/if}

			{#if showTrailing && trailingItem}
				{@const trailing = trailingItem}
				{@const TrailingIcon = trailingIcon ?? itemIcon}
				<button
					type="button"
					class="menu-item trailing-item"
					{...rowProps('trailing', trailing.id)}
					onclick={() => handleSelect(trailing.id)}
				>
					{#if TrailingIcon}<TrailingIcon size={16} strokeWidth={1.5} />{/if}
					<span class="item-name">{trailing.name}</span>
					{#if numbered && !query}<kbd class="row-key" aria-hidden="true">0</kbd>{/if}
				</button>
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
		inert={activeGroup === null}
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
					{@render itemLabel(item)}
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
	/* `clip`, not only `hidden`: a hidden box can still be scrolled sideways by
	   focus, which would slide the off-screen panel into view. */
	.grouped-selector {
		position: relative;
		overflow: hidden;
		overflow: clip;
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
		max-height: var(--selector-scroll-height, min(40vh, 320px));
		overflow-y: auto;
		overscroll-behavior: contain;
		touch-action: pan-y;
		-webkit-overflow-scrolling: touch;
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

	.item-copy {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 1px;
	}

	.item-desc {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.8em;
		color: var(--muted-foreground);
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

	/* Search (searchable) */
	.search-row {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0 0 4px;
		padding: 6px 10px;
		border-bottom: 1px solid var(--border-subtle);
		color: var(--muted-foreground);
	}

	.search-row :global(svg) {
		flex-shrink: 0;
	}

	.search-input {
		flex: 1;
		min-width: 0;
		padding: 2px 0;
		background: transparent;
		border: 0;
		outline: none;
		color: var(--foreground);
		font-size: 0.875rem;
	}

	.search-input::placeholder {
		color: var(--subtle-foreground);
	}

	.menu-item[data-highlighted] {
		background: var(--hover-overlay);
	}

	.menu-item.selected[data-highlighted] {
		background: color-mix(in oklch, var(--primary) 12%, transparent);
	}

	.trailing-item {
		color: var(--muted-foreground);
	}

	.row-key {
		flex-shrink: 0;
		min-width: 18px;
		padding: 0 4px;
		border: 1px solid var(--border-subtle);
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		color: var(--subtle-foreground);
		font-family: var(--font-mono, ui-monospace, monospace);
		font-size: 0.6875rem;
		line-height: 16px;
		text-align: center;
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
