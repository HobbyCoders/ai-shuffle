<script lang="ts">
	/**
	 * DropdownContextMenu - Right-click context menu for dropdown items
	 *
	 * Provides CRUD operations and group management for profiles/projects
	 * Organized into logical sections:
	 * - Quick Actions (Select, Edit, Duplicate)
	 * - Group Management (Assign to group, Create group, Remove from group)
	 * - Danger Zone (Delete)
	 */
	import { createEventDispatcher } from 'svelte';
	import { groups, type EntityType } from '$lib/stores/groups';

	type ItemType = 'profile' | 'project';

	interface Props {
		show: boolean;
		x: number;
		y: number;
		itemId: string;
		itemName: string;
		itemType: ItemType;
		isSelected?: boolean;
		isBuiltin?: boolean;
		currentGroup?: string;
		onClose: () => void;
	}

	let {
		show,
		x,
		y,
		itemId,
		itemName,
		itemType,
		isSelected = false,
		isBuiltin = false,
		currentGroup,
		onClose
	}: Props = $props();

	const dispatch = createEventDispatcher<{
		select: { id: string };
		edit: { id: string };
		duplicate: { id: string };
		delete: { id: string };
		openCard: { type: ItemType; editId?: string };
		assignGroup: { id: string; groupName: string };
		removeFromGroup: { id: string };
		createGroup: { id: string };
	}>();

	// Get available groups for this entity type
	const entityType: EntityType = $derived(itemType === 'profile' ? 'profiles' : 'projects');
	const availableGroups = $derived($groups[entityType].groups);

	// Adjust menu position to stay within viewport
	let menuEl: HTMLDivElement | undefined = $state();
	let adjustedX = $state(x);
	let adjustedY = $state(y);

	$effect(() => {
		if (show && menuEl) {
			const rect = menuEl.getBoundingClientRect();
			const viewportWidth = window.innerWidth;
			const viewportHeight = window.innerHeight;

			// Adjust horizontal position
			if (x + rect.width > viewportWidth - 10) {
				adjustedX = viewportWidth - rect.width - 10;
			} else {
				adjustedX = x;
			}

			// Adjust vertical position
			if (y + rect.height > viewportHeight - 10) {
				adjustedY = viewportHeight - rect.height - 10;
			} else {
				adjustedY = y;
			}
		}
	});

	function handleSelect() {
		dispatch('select', { id: itemId });
		onClose();
	}

	function handleEdit() {
		dispatch('edit', { id: itemId });
		onClose();
	}

	function handleOpenCard(editId?: string) {
		dispatch('openCard', { type: itemType, editId });
		onClose();
	}

	function handleDuplicate() {
		dispatch('duplicate', { id: itemId });
		onClose();
	}

	function handleDelete() {
		dispatch('delete', { id: itemId });
		onClose();
	}

	function handleAssignGroup(groupName: string) {
		groups.assignToGroup(entityType, itemId, groupName);
		dispatch('assignGroup', { id: itemId, groupName });
		onClose();
	}

	function handleRemoveFromGroup() {
		groups.removeFromGroup(entityType, itemId);
		dispatch('removeFromGroup', { id: itemId });
		onClose();
	}

	function handleCreateGroup() {
		const name = prompt('New group name:');
		if (name?.trim()) {
			groups.createGroup(entityType, name.trim());
			groups.assignToGroup(entityType, itemId, name.trim());
			dispatch('createGroup', { id: itemId });
		}
		onClose();
	}

	function handleDeleteGroup(groupName: string) {
		const confirmDelete = confirm(
			`Delete group "${groupName}"?\n\nThis will remove the group and unassign all ${itemType}s from it. The ${itemType}s themselves will not be deleted.`
		);
		if (confirmDelete) {
			groups.deleteGroup(entityType, groupName);
		}
		onClose();
	}

	// Close on click outside
	function handleBackdropClick(e: MouseEvent) {
		if (e.target === e.currentTarget) {
			onClose();
		}
	}

	// Close on escape
	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			onClose();
		}
	}
</script>

<svelte:window onkeydown={handleKeydown} />

{#if show}
	<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
	<div class="context-menu-backdrop" onclick={handleBackdropClick}>
		<div
			bind:this={menuEl}
			class="context-menu"
			style:left="{adjustedX}px"
			style:top="{adjustedY}px"
		>
			<!-- Header with item name -->
			<div class="menu-header">
				<span class="header-icon">
					{#if itemType === 'profile'}
						<svg class="icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
						</svg>
					{:else}
						<svg class="icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
						</svg>
					{/if}
				</span>
				<span class="header-name">{itemName}</span>
				{#if isBuiltin}
					<span class="header-badge">Built-in</span>
				{/if}
			</div>

			<div class="menu-divider"></div>

			<!-- Quick Actions -->
			<div class="menu-section">
				<button class="menu-item" onclick={handleSelect}>
					<svg class="item-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
					</svg>
					<span class="item-label">Select for this chat</span>
					{#if isSelected}
						<span class="item-badge active">Active</span>
					{/if}
				</button>

				{#if !isBuiltin}
					<button class="menu-item" onclick={handleEdit}>
						<svg class="item-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
						</svg>
						<span class="item-label">Edit {itemType}</span>
					</button>
				{/if}

				{#if itemType === 'profile' && !isBuiltin}
					<button class="menu-item" onclick={handleDuplicate}>
						<svg class="item-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
						</svg>
						<span class="item-label">Duplicate</span>
					</button>
				{/if}
			</div>

			<div class="menu-divider"></div>

			<!-- Card Management -->
			<div class="menu-section">
				<div class="section-label">Open Card</div>
				<button class="menu-item" onclick={() => handleOpenCard()}>
					<svg class="item-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
					</svg>
					<span class="item-label">View all {itemType}s</span>
				</button>
				{#if !isBuiltin}
					<button class="menu-item" onclick={() => handleOpenCard(itemId)}>
						<svg class="item-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
						</svg>
						<span class="item-label">Edit in card</span>
					</button>
				{/if}
			</div>

			<div class="menu-divider"></div>

			<!-- Group Management -->
			<div class="menu-section">
				<div class="section-label">Group</div>

				{#if availableGroups.length > 0}
					<!-- Submenu for existing groups -->
					<div class="submenu-container">
						<button class="menu-item has-submenu">
							<svg class="item-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" />
							</svg>
							<span class="item-label">Move to group</span>
							<svg class="item-chevron" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
							</svg>
						</button>
						<div class="submenu">
							{#each availableGroups as group}
								<button
									class="menu-item"
									class:selected={currentGroup === group.name}
									onclick={() => handleAssignGroup(group.name)}
								>
									{#if currentGroup === group.name}
										<svg class="item-icon check" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
										</svg>
									{:else}
										<span class="item-spacer"></span>
									{/if}
									<span class="item-label">{group.name}</span>
								</button>
							{/each}
						</div>
					</div>
				{/if}

				<button class="menu-item" onclick={handleCreateGroup}>
					<svg class="item-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
					</svg>
					<span class="item-label">Create new group...</span>
				</button>

				{#if currentGroup}
					<button class="menu-item muted" onclick={handleRemoveFromGroup}>
						<svg class="item-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
						<span class="item-label">Remove from "{currentGroup}"</span>
					</button>
				{/if}

				{#if availableGroups.length > 0}
					<!-- Submenu for deleting groups -->
					<div class="submenu-container">
						<button class="menu-item has-submenu danger-subtle">
							<svg class="item-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
							</svg>
							<span class="item-label">Delete group</span>
							<svg class="item-chevron" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
							</svg>
						</button>
						<div class="submenu">
							{#each availableGroups as group}
								<button
									class="menu-item danger"
									onclick={() => handleDeleteGroup(group.name)}
								>
									<svg class="item-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
									</svg>
									<span class="item-label">{group.name}</span>
								</button>
							{/each}
						</div>
					</div>
				{/if}
			</div>

			{#if !isBuiltin}
				<div class="menu-divider"></div>

				<!-- Danger Zone -->
				<div class="menu-section">
					<button class="menu-item danger" onclick={handleDelete}>
						<svg class="item-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
						</svg>
						<span class="item-label">Delete {itemType}</span>
					</button>
				</div>
			{/if}
		</div>
	</div>
{/if}

<style>
	.context-menu-backdrop {
		position: fixed;
		inset: 0;
		z-index: 9999;
	}

	.context-menu {
		position: fixed;
		min-width: 220px;
		max-width: 280px;
		background: hsl(var(--card));
		border: 1px solid hsl(var(--border));
		border-radius: 12px;
		box-shadow:
			0 10px 38px -10px rgba(0, 0, 0, 0.35),
			0 10px 20px -15px rgba(0, 0, 0, 0.2);
		padding: 6px;
		animation: slideIn 0.15s ease-out;
		backdrop-filter: none;
	}

	@keyframes slideIn {
		from {
			opacity: 0;
			transform: scale(0.96) translateY(-4px);
		}
		to {
			opacity: 1;
			transform: scale(1) translateY(0);
		}
	}

	/* Header */
	.menu-header {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 10px;
	}

	.header-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 24px;
		height: 24px;
		border-radius: 6px;
		background: hsl(var(--primary) / 0.1);
		color: hsl(var(--primary));
		flex-shrink: 0;
	}

	.header-icon .icon {
		width: 14px;
		height: 14px;
	}

	.header-name {
		font-size: 0.8125rem;
		font-weight: 600;
		color: hsl(var(--foreground));
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		flex: 1;
	}

	.header-badge {
		font-size: 0.625rem;
		padding: 2px 6px;
		border-radius: 4px;
		background: hsl(var(--muted));
		color: hsl(var(--muted-foreground));
		text-transform: uppercase;
		letter-spacing: 0.02em;
	}

	/* Divider */
	.menu-divider {
		height: 1px;
		background: hsl(var(--border));
		margin: 4px 0;
	}

	/* Section */
	.menu-section {
		padding: 2px 0;
	}

	.section-label {
		padding: 6px 10px 4px;
		font-size: 0.6875rem;
		font-weight: 500;
		color: hsl(var(--muted-foreground));
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	/* Menu Items */
	.menu-item {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 8px 10px;
		background: transparent;
		border: none;
		border-radius: 8px;
		color: hsl(var(--foreground));
		font-size: 0.8125rem;
		cursor: pointer;
		transition: all 0.1s ease;
		text-align: left;
	}

	.menu-item:hover {
		background: hsl(var(--accent));
	}

	.menu-item.selected {
		background: hsl(var(--primary) / 0.1);
	}

	.menu-item.muted {
		color: hsl(var(--muted-foreground));
	}

	.menu-item.danger {
		color: hsl(var(--destructive));
	}

	.menu-item.danger:hover {
		background: hsl(var(--destructive) / 0.1);
	}

	.menu-item.danger-subtle {
		color: hsl(var(--muted-foreground));
	}

	.menu-item.danger-subtle:hover {
		color: hsl(var(--destructive));
		background: hsl(var(--destructive) / 0.1);
	}

	.menu-item.danger-subtle:hover .item-icon {
		color: hsl(var(--destructive));
	}

	.item-icon {
		width: 16px;
		height: 16px;
		flex-shrink: 0;
		color: hsl(var(--muted-foreground));
	}

	.menu-item:hover .item-icon {
		color: hsl(var(--foreground));
	}

	.menu-item.danger .item-icon {
		color: hsl(var(--destructive));
	}

	.item-icon.check {
		color: hsl(var(--primary));
	}

	.item-spacer {
		width: 16px;
		flex-shrink: 0;
	}

	.item-label {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.item-badge {
		font-size: 0.625rem;
		padding: 2px 6px;
		border-radius: 4px;
		background: hsl(var(--muted));
		color: hsl(var(--muted-foreground));
	}

	.item-badge.active {
		background: hsl(var(--primary) / 0.15);
		color: hsl(var(--primary));
	}

	.item-chevron {
		width: 14px;
		height: 14px;
		color: hsl(var(--muted-foreground));
		flex-shrink: 0;
	}

	/* Submenu */
	.submenu-container {
		position: relative;
	}

	.has-submenu {
		position: relative;
	}

	.submenu {
		position: absolute;
		left: 100%;
		top: 0;
		min-width: 160px;
		margin-left: 4px;
		background: hsl(var(--card));
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
		box-shadow:
			0 10px 38px -10px rgba(0, 0, 0, 0.35),
			0 10px 20px -15px rgba(0, 0, 0, 0.2);
		padding: 4px;
		opacity: 0;
		visibility: hidden;
		transform: translateX(-4px);
		backdrop-filter: none;
		transition: all 0.15s ease;
		max-height: 200px;
		overflow-y: auto;
	}

	.submenu-container:hover .submenu {
		opacity: 1;
		visibility: visible;
		transform: translateX(0);
	}
</style>
