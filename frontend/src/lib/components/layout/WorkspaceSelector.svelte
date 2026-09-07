<script lang="ts">
	/**
	 * WorkspaceSelector - "Let's build [workspace]" empty state
	 *
	 * Codex-style workspace picker shown when starting a new chat.
	 * Centered hero with clickable workspace name dropdown.
	 * Uses GroupedSelector for group-aware project selection.
	 */
	import { ChevronDown, FolderClosed } from 'lucide-svelte';
	import { isMobileUi } from '$lib/stores/platform.svelte';
	import { appData, clusterProjects, profiles, projects } from '$lib/stores/app-data.svelte';
	import AddProjectModal from '$lib/components/AddProjectModal.svelte';
	import GroupedSelector from '$lib/components/GroupedSelector.svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import {
		buildWorkspacePickerItems,
		pickerItemName,
		selectedPickerId
	} from './workspace-catalog';

	interface Props {
		selectedProject: string | null;
		selectedProjectV2Id?: string | null;
		onSelectProject: (projectId: string, projectV2Id?: string | null) => void;
	}

	let { selectedProject, selectedProjectV2Id = null, onSelectProject }: Props = $props();

	let showDropdown = $state(false);
	let dropdownRef = $state<HTMLDivElement | null>(null);
	let groupedSelector = $state<ReturnType<typeof GroupedSelector> | null>(null);
	const pickerId = $props.id();

	// On the mobile shell use the top layer so neither the chat surface nor the
	// keyboard can clip this popup. The desktop shell keeps its anchored dropdown
	// at every width.
	function mobilePopup(node: HTMLDivElement) {
		const viewport = window.visualViewport;
		const scrollport = dropdownRef?.closest<HTMLElement>('.chat-view');
		let topLayer = false;
		function position() {
			if (!isMobileUi()) {
				if (topLayer) node.hidePopover();
				topLayer = false;
				node.removeAttribute('popover');
				node.classList.remove('mobile-popup');
				for (const property of ['--selector-scroll-height', '--picker-left', '--picker-top']) node.style.removeProperty(property);
				return;
			}
			node.classList.add('mobile-popup');
			if (!topLayer && typeof node.showPopover === 'function') {
				node.setAttribute('popover', 'manual');
				node.showPopover();
				topLayer = true;
			}
			const anchor = dropdownRef?.getBoundingClientRect();
			if (!anchor) return;
			const bounds = scrollport?.getBoundingClientRect();
			if (bounds && (anchor.bottom <= bounds.top || anchor.top >= bounds.bottom)) {
				showDropdown = false;
				return;
			}
			const edge = 12;
			const viewTop = viewport?.offsetTop ?? 0;
			const viewBottom = viewTop + (viewport?.height ?? window.innerHeight);
			const below = viewBottom - anchor.bottom - edge - 8;
			const above = anchor.top - viewTop - edge - 8;
			const upwards = below < 180 && above > below;
			const available = upwards ? above : below;
			// Header, divider, footer and padding use at most 100px; only the
			// shared list scrolls. Recompute as the visual viewport changes.
			node.style.setProperty('--selector-scroll-height', `${Math.min(320, Math.max(44, available - 100))}px`);
			const rect = node.getBoundingClientRect();
			const left = Math.max(edge, Math.min(window.innerWidth - rect.width - edge, anchor.left + (anchor.width - rect.width) / 2));
			const top = Math.max(viewTop + edge, Math.min(viewBottom - rect.height - edge, upwards ? anchor.top - rect.height - 8 : anchor.bottom + 8));
			node.style.setProperty('--picker-left', `${left}px`);
			node.style.setProperty('--picker-top', `${top}px`);
		}
		const observer = new ResizeObserver(position);
		observer.observe(node);
		const hero = dropdownRef?.closest('.empty-hero');
		if (hero) observer.observe(hero);
		scrollport?.addEventListener('scroll', position);
		window.addEventListener('resize', position);
		viewport?.addEventListener('resize', position);
		viewport?.addEventListener('scroll', position);
		position();
		return { destroy() {
			observer.disconnect();
			scrollport?.removeEventListener('scroll', position);
			window.removeEventListener('resize', position);
			viewport?.removeEventListener('resize', position);
			viewport?.removeEventListener('scroll', position);
		} };
	}

	function closeOnEscape(event: KeyboardEvent) {
		if (event.key !== 'Escape' || !showDropdown) return;
		event.preventDefault();
		showDropdown = false;
		dropdownRef?.querySelector('button')?.focus({ preventScroll: true });
	}

	// Add project modal state
	let showAddModal = $state(false);

	const pickerItems = $derived(buildWorkspacePickerItems($projects, $clusterProjects));
	const selectedId = $derived(
		selectedPickerId(selectedProject, selectedProjectV2Id, pickerItems)
	);
	const selectedProjectName = $derived(
		pickerItemName(pickerItems, selectedId) ||
			$projects.find((p) => p.id === selectedProject)?.name ||
			'your project'
	);

	// Close dropdown on outside click
	function handleClickOutside(e: MouseEvent) {
		if (dropdownRef && !dropdownRef.contains(e.target as Node)) {
			showDropdown = false;
		}
	}

	function handleSelect(id: string) {
		const item = pickerItems.find((i) => i.id === id);
		if (item) {
			onSelectProject(item.project, item.projectV2Id);
		} else {
			onSelectProject(id);
		}
		showDropdown = false;
	}

	function openAddModal() {
		showDropdown = false;
		showAddModal = true;
	}

	function toggleDropdown() {
		showDropdown = !showDropdown;
		if (showDropdown) {
			// Reset grouped selector to main panel when opening
			groupedSelector?.reset();
			void appData.loadClusterProjects();
		}
	}
</script>

<svelte:window onclick={handleClickOutside} onkeydown={closeOnEscape} />

<div class="workspace-selector">
	<!-- Logo / Icon -->
	<div class="logo-mark">
		<svg width="40" height="40" viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg">
			<path d="M20 4C11.16 4 4 11.16 4 20s7.16 16 16 16 16-7.16 16-16S28.84 4 20 4zm0 28c-6.63 0-12-5.37-12-12S13.37 8 20 8s12 5.37 12 12-5.37 12-12 12z" fill="currentColor" opacity="0.2"/>
			<path d="M15 17c0-1.1.9-2 2-2h6c1.1 0 2 .9 2 2v6c0 1.1-.9 2-2 2h-6c-1.1 0-2-.9-2-2v-6z" fill="currentColor" opacity="0.5"/>
			<circle cx="20" cy="20" r="3" fill="currentColor"/>
		</svg>
	</div>

	<!-- Heading -->
	<h1 class="hero-heading">Let's build</h1>

	<!-- Workspace Picker -->
	<div class="picker-container" bind:this={dropdownRef}>
		<button
			type="button"
			class="picker-trigger"
			aria-expanded={showDropdown}
			aria-controls={pickerId}
			onclick={toggleDropdown}
		>
			<span class="picker-name">{selectedProjectName}</span>
			<ChevronDown
				size={18}
				strokeWidth={1.5}
				class="picker-chevron {showDropdown ? 'flipped' : ''}"
			/>
		</button>

		{#if showDropdown}
			<div id={pickerId} class="picker-dropdown" use:mobilePopup>
				<GroupedSelector
					bind:this={groupedSelector}
					items={pickerItems}
					entityType="projects"
					selectedId={selectedId}
					onSelect={handleSelect}
					label="Select your workspace"
					addLabel="Add Project"
					onAdd={openAddModal}
					emptyMessage="No workspaces available"
					itemIcon={FolderClosed}
				/>
			</div>
		{/if}
	</div>

	{#if $profiles.length === 0}
		<!-- Nothing is seeded, so a fresh install lands here with an empty agent
		     picker and a send that can only fail. Say what is missing where the
		     user is already looking, rather than at submit time. -->
		<button type="button" class="no-agents" onclick={() => layout.openFeaturePage('profile')}>
			No agents yet — create one to start chatting
		</button>
	{/if}
</div>

<!-- Add Project Modal -->
<AddProjectModal
	open={showAddModal}
	onClose={() => (showAddModal = false)}
	onCreated={(id) => onSelectProject(id)}
/>

<style>
	.workspace-selector {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 4px;
		padding: var(--workspace-hero-padding, 2rem);
		user-select: none;
	}

	/* Logo mark */
	.logo-mark {
		color: var(--foreground);
		margin-bottom: 8px;
		opacity: 0.8;
	}

	/* Hero heading */
	.hero-heading {
		font-family: var(--font-display, Georgia, serif);
		font-size: 2rem;
		font-weight: 700;
		color: var(--foreground);
		letter-spacing: -0.02em;
		line-height: 1.1;
	}

	/* Workspace picker trigger */
	.picker-container {
		position: relative;
	}

	.no-agents {
		margin-top: 4px;
		background: none;
		border: none;
		padding: 0;
		color: var(--primary);
		cursor: pointer;
		font-size: 0.8125rem;
	}

	.no-agents:hover {
		text-decoration: underline;
	}

	.picker-trigger {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 4px 8px;
		background: transparent;
		border: none;
		cursor: pointer;
		transition: opacity 150ms ease;
	}

	.picker-trigger:hover {
		opacity: 0.7;
	}

	.picker-name {
		font-family: var(--font-display, Georgia, serif);
		font-size: 1.75rem;
		font-weight: 400;
		color: var(--muted-foreground);
		letter-spacing: -0.01em;
	}

	.picker-trigger :global(.picker-chevron) {
		color: var(--muted-foreground);
		transition: transform 200ms ease;
	}

	.picker-trigger :global(.picker-chevron.flipped) {
		transform: rotate(180deg);
	}

	/* Dropdown */
	.picker-dropdown {
		position: absolute;
		top: calc(100% + 8px);
		left: 50%;
		transform: translateX(-50%);
		min-width: 280px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		box-shadow: var(--shadow-l);
		padding: 8px;
		z-index: 100;
		animation: dropdownAppear 150ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes dropdownAppear {
		from {
			opacity: 0;
			transform: translateX(-50%) translateY(-4px) scale(0.98);
		}
		to {
			opacity: 1;
			transform: translateX(-50%) translateY(0) scale(1);
		}
	}

	/* Reduced motion */
	.picker-dropdown:global(.mobile-popup) {
		position: fixed;
		inset: auto;
		top: var(--picker-top);
		left: var(--picker-left);
		width: min(320px, calc(100vw - 24px));
		min-width: 0;
		margin: 0;
		transform: none;
		animation: none;
	}

	@media (prefers-reduced-motion: reduce) {
		.picker-dropdown {
			animation: none;
		}
	}
</style>
