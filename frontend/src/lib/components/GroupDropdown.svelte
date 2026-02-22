<script lang="ts">
	/**
	 * GroupDropdown - Portal-based dropdown for group assignment
	 * Renders dropdown to document.body to escape parent transform/overflow clipping
	 */
	import { onMount, onDestroy, tick } from 'svelte';
	import { groups as groupsStore } from '$lib/stores/groups';

	interface Props {
		entityType: 'profiles' | 'projects' | 'subagents';
		entityId: string;
	}

	let { entityType, entityId }: Props = $props();

	let isOpen = $state(false);
	let isCreating = $state(false);
	let newGroupName = $state('');
	let buttonRef: HTMLButtonElement | undefined = $state();
	let portalContainer: HTMLDivElement | null = $state(null);
	let dropdownPosition = $state({ top: 0, left: 0 });

	// Get groups data from store
	const groups = $derived($groupsStore);

	// Get current entity's group assignment
	const currentGroup = $derived(groups?.[entityType]?.assignments?.[entityId] ?? null);
	const availableGroups = $derived(groups?.[entityType]?.groups ?? []);

	function toggle(e: MouseEvent) {
		e.stopPropagation();
		if (isOpen) {
			closeDropdown();
		} else {
			updatePosition();
			isOpen = true;
			isCreating = false;
			newGroupName = '';
			renderPortal();
		}
	}

	function updatePosition() {
		if (!buttonRef) return;
		const rect = buttonRef.getBoundingClientRect();
		dropdownPosition = {
			top: rect.bottom + 4,
			left: Math.max(8, rect.right - 180)
		};
	}

	function closeDropdown() {
		isOpen = false;
		isCreating = false;
		newGroupName = '';
		removePortal();
	}

	function handleClickOutside(e: MouseEvent) {
		if (!isOpen) return;
		const target = e.target as HTMLElement;
		if (!buttonRef?.contains(target) && !portalContainer?.contains(target)) {
			closeDropdown();
		}
	}

	function assignToGroup(groupName: string) {
		groupsStore.assignToGroup(entityType, entityId, groupName);
		closeDropdown();
	}

	function removeFromGroup() {
		groupsStore.removeFromGroup(entityType, entityId);
		closeDropdown();
	}

	function deleteGroup(groupName: string) {
		groupsStore.deleteGroup(entityType, groupName);
		// Re-render portal to reflect changes
		updatePortalContent();
	}

	function startCreating() {
		isCreating = true;
		newGroupName = '';
		updatePortalContent();
		// Focus the input after render
		requestAnimationFrame(() => {
			const input = portalContainer?.querySelector('.group-name-input') as HTMLInputElement;
			input?.focus();
		});
	}

	function confirmCreate() {
		const name = newGroupName.trim();
		if (name) {
			groupsStore.createGroup(entityType, name);
			groupsStore.assignToGroup(entityType, entityId, name);
			closeDropdown();
		}
	}

	function cancelCreate() {
		isCreating = false;
		newGroupName = '';
		updatePortalContent();
	}

	function renderPortal() {
		if (portalContainer) return;

		portalContainer = document.createElement('div');
		portalContainer.className = 'group-dropdown-portal-root';
		document.body.appendChild(portalContainer);

		updatePortalContent();
	}

	function updatePortalContent() {
		if (!portalContainer) return;

		const groupItems = availableGroups.map(group => `
			<div class="dropdown-item-row">
				<button class="dropdown-item" data-action="assign" data-group="${group.name}">
					${currentGroup === group.name
						? `<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
						</svg>`
						: `<span class="spacer"></span>`
					}
					${group.name}
				</button>
				<button class="dropdown-delete-btn" data-action="delete-group" data-group="${group.name}" title="Delete group">
					<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" width="12" height="12">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>
		`).join('');

		const removeItem = currentGroup ? `
			<button class="dropdown-item muted" data-action="remove">
				<span class="spacer"></span>
				Remove from group
			</button>
		` : '';

		const createSection = isCreating ? `
			<div class="dropdown-divider"></div>
			<div class="group-create-row">
				<input
					type="text"
					class="group-name-input"
					placeholder="Group name..."
					value="${newGroupName}"
					maxlength="30"
				/>
				<button class="group-create-confirm" data-action="confirm-create" title="Create">
					<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" width="14" height="14">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
					</svg>
				</button>
				<button class="group-create-cancel" data-action="cancel-create" title="Cancel">
					<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" width="14" height="14">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>
		` : `
			<div class="dropdown-divider"></div>
			<button class="dropdown-item muted" data-action="start-create">
				<span class="spacer"></span>
				+ New group
			</button>
		`;

		portalContainer.innerHTML = `
			<div class="group-dropdown-portal" style="top: ${dropdownPosition.top}px; left: ${dropdownPosition.left}px;">
				${groupItems}
				${removeItem}
				${createSection}
			</div>
		`;

		// Add event listeners
		portalContainer.querySelectorAll('[data-action]').forEach(btn => {
			btn.addEventListener('click', (e) => {
				e.stopPropagation();
				const action = (btn as HTMLElement).dataset.action;
				if (action === 'assign') {
					const groupName = (btn as HTMLElement).dataset.group;
					if (groupName) assignToGroup(groupName);
				} else if (action === 'remove') {
					removeFromGroup();
				} else if (action === 'start-create') {
					startCreating();
				} else if (action === 'confirm-create') {
					confirmCreate();
				} else if (action === 'cancel-create') {
					cancelCreate();
				} else if (action === 'delete-group') {
					const groupName = (btn as HTMLElement).dataset.group;
					if (groupName) deleteGroup(groupName);
				}
			});
		});

		// Handle input events for the create input
		const input = portalContainer.querySelector('.group-name-input') as HTMLInputElement;
		if (input) {
			input.addEventListener('input', (e) => {
				newGroupName = (e.target as HTMLInputElement).value;
			});
			input.addEventListener('keydown', (e) => {
				if (e.key === 'Enter') {
					e.preventDefault();
					confirmCreate();
				} else if (e.key === 'Escape') {
					e.preventDefault();
					cancelCreate();
				}
			});
		}
	}

	function removePortal() {
		if (portalContainer) {
			portalContainer.remove();
			portalContainer = null;
		}
	}

	onMount(() => {
		document.addEventListener('click', handleClickOutside);
		window.addEventListener('scroll', closeDropdown, true);
		window.addEventListener('resize', closeDropdown);
	});

	onDestroy(() => {
		document.removeEventListener('click', handleClickOutside);
		window.removeEventListener('scroll', closeDropdown, true);
		window.removeEventListener('resize', closeDropdown);
		removePortal();
	});
</script>

<!-- Trigger button -->
<button
	bind:this={buttonRef}
	class="card-action-btn"
	class:active={isOpen}
	title="Assign to group"
	onclick={toggle}
>
	<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
		<path
			stroke-linecap="round"
			stroke-linejoin="round"
			stroke-width="2"
			d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z"
		/>
	</svg>
</button>

<!-- Dropdown is rendered via portal to document.body -->

<style>
	/* Scoped styles for the trigger button */
	.card-action-btn {
		width: 28px;
		height: 28px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.card-action-btn:hover {
		background: var(--hover-overlay, oklch(1 0 0 / 0.1));
		color: var(--foreground);
	}

	.card-action-btn.active {
		background: var(--selection-bg-emphasis);
		color: var(--selection-text);
	}

	.card-action-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.card-action-btn svg {
		width: 16px;
		height: 16px;
	}

	/* Global styles for portal-rendered dropdown (must be :global since it renders to document.body) */
	:global(.group-dropdown-portal-root) {
		position: fixed;
		top: 0;
		left: 0;
		z-index: 99999;
		pointer-events: none;
	}

	:global(.group-dropdown-portal) {
		position: fixed;
		z-index: 99999;
		min-width: 180px;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
		padding: 4px;
		pointer-events: auto;
		animation: groupDropdownFadeIn 0.15s ease;
	}

	@keyframes groupDropdownFadeIn {
		from {
			opacity: 0;
			transform: translateY(-4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	:global(.group-dropdown-portal .dropdown-item-row) {
		display: flex;
		align-items: center;
	}

	:global(.group-dropdown-portal .dropdown-item) {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1;
		padding: 8px 12px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		color: var(--foreground);
		font-size: 0.8125rem;
		text-align: left;
		cursor: pointer;
		transition: background 0.1s ease;
	}

	:global(.group-dropdown-portal .dropdown-item-row .dropdown-item) {
		padding-right: 4px;
	}

	:global(.group-dropdown-portal .dropdown-item:hover) {
		background: var(--hover-overlay, oklch(1 0 0 / 0.1));
	}

	:global(.group-dropdown-portal .dropdown-item.selected) {
		background: var(--selection-bg);
		color: var(--selection-text);
	}

	:global(.group-dropdown-portal .dropdown-item:focus-visible) {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	:global(.group-dropdown-portal .dropdown-item.muted) {
		color: var(--muted-foreground);
	}

	:global(.group-dropdown-portal .dropdown-delete-btn) {
		width: 24px;
		height: 24px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		color: var(--muted-foreground);
		cursor: pointer;
		opacity: 0;
		transition: all 0.1s ease;
		margin-right: 4px;
		flex-shrink: 0;
	}

	:global(.group-dropdown-portal .dropdown-item-row:hover .dropdown-delete-btn) {
		opacity: 1;
	}

	:global(.group-dropdown-portal .dropdown-delete-btn:hover) {
		background: var(--destructive, #ef4444);
		color: white;
		opacity: 1;
	}

	:global(.group-dropdown-portal .dropdown-divider) {
		height: 1px;
		background: var(--border);
		margin: 4px 0;
	}

	:global(.group-dropdown-portal .check-icon) {
		width: 14px;
		height: 14px;
		color: var(--primary);
	}

	:global(.group-dropdown-portal .spacer) {
		display: inline-block;
		width: 14px;
	}

	/* Inline group creation input */
	:global(.group-dropdown-portal .group-create-row) {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 4px;
	}

	:global(.group-dropdown-portal .group-name-input) {
		flex: 1;
		min-width: 0;
		padding: 6px 8px;
		background: var(--surface-0, var(--background));
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		color: var(--foreground);
		font-size: 0.8125rem;
		outline: none;
		transition: border-color 0.15s ease;
	}

	:global(.group-dropdown-portal .group-name-input:focus) {
		border-color: var(--primary);
	}

	:global(.group-dropdown-portal .group-name-input::placeholder) {
		color: var(--muted-foreground);
	}

	:global(.group-dropdown-portal .group-create-confirm),
	:global(.group-dropdown-portal .group-create-cancel) {
		width: 26px;
		height: 26px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		cursor: pointer;
		transition: all 0.1s ease;
		flex-shrink: 0;
	}

	:global(.group-dropdown-portal .group-create-confirm) {
		color: var(--primary);
	}

	:global(.group-dropdown-portal .group-create-confirm:hover) {
		background: var(--primary);
		color: white;
	}

	:global(.group-dropdown-portal .group-create-cancel) {
		color: var(--muted-foreground);
	}

	:global(.group-dropdown-portal .group-create-cancel:hover) {
		background: var(--hover-overlay, oklch(1 0 0 / 0.1));
		color: var(--foreground);
	}
</style>
