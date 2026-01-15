<script lang="ts">
	/**
	 * GroupDropdown - Portal-based dropdown for group assignment
	 * Renders dropdown to document.body to escape parent transform/overflow clipping
	 */
	import { onMount, onDestroy } from 'svelte';
	import { groups as groupsStore } from '$lib/stores/groups';

	interface Props {
		entityType: 'profiles' | 'projects' | 'subagents';
		entityId: string;
	}

	let { entityType, entityId }: Props = $props();

	let isOpen = $state(false);
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
			renderPortal();
		}
	}

	function updatePosition() {
		if (!buttonRef) return;
		const rect = buttonRef.getBoundingClientRect();
		dropdownPosition = {
			top: rect.bottom + 4,
			left: Math.max(8, rect.right - 160) // Align right edge with button, but keep on screen
		};
	}

	function closeDropdown() {
		isOpen = false;
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

	function createGroup() {
		const name = prompt('New group name:');
		if (name?.trim()) {
			groupsStore.createGroup(entityType, name.trim());
			groupsStore.assignToGroup(entityType, entityId, name.trim());
		}
		closeDropdown();
	}

	function renderPortal() {
		if (portalContainer) return;

		portalContainer = document.createElement('div');
		portalContainer.className = 'group-dropdown-portal-root';
		document.body.appendChild(portalContainer);

		// Force re-render to update the portal content
		updatePortalContent();
	}

	function updatePortalContent() {
		if (!portalContainer) return;

		const groupItems = availableGroups.map(group => `
			<button class="dropdown-item" data-action="assign" data-group="${group.name}">
				${currentGroup === group.name
					? `<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
					</svg>`
					: `<span class="spacer"></span>`
				}
				${group.name}
			</button>
		`).join('');

		const removeItem = currentGroup ? `
			<button class="dropdown-item muted" data-action="remove">
				<span class="spacer"></span>
				Remove
			</button>
		` : '';

		portalContainer.innerHTML = `
			<div class="group-dropdown-portal" style="top: ${dropdownPosition.top}px; left: ${dropdownPosition.left}px;">
				${groupItems}
				${removeItem}
				<div class="dropdown-divider"></div>
				<button class="dropdown-item muted" data-action="create">
					<span class="spacer"></span>
					+ New group
				</button>
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
				} else if (action === 'create') {
					createGroup();
				}
			});
		});
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
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
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
		min-width: 160px;
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

	:global(.group-dropdown-portal .dropdown-item) {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
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

	:global(.group-dropdown-portal .dropdown-item:hover) {
		background: var(--hover-overlay, oklch(1 0 0 / 0.1));
	}

	:global(.group-dropdown-portal .dropdown-item.selected) {
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		color: var(--primary);
	}

	:global(.group-dropdown-portal .dropdown-item:focus-visible) {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	:global(.group-dropdown-portal .dropdown-item.muted) {
		color: var(--muted-foreground);
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
</style>
