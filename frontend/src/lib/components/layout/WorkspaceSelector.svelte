<script lang="ts">
	/**
	 * WorkspaceSelector - "Let's build [workspace]" empty state
	 *
	 * Codex-style workspace picker shown when starting a new chat.
	 * Centered hero with clickable workspace name dropdown.
	 * Uses GroupedSelector for group-aware project selection.
	 */
	import { ChevronDown, FolderClosed } from 'lucide-svelte';
	import { projects } from '$lib/stores/app-data';
	import AddProjectModal from '$lib/components/AddProjectModal.svelte';
	import GroupedSelector from '$lib/components/GroupedSelector.svelte';

	interface Props {
		selectedProject: string | null;
		onSelectProject: (projectId: string) => void;
	}

	let { selectedProject, onSelectProject }: Props = $props();

	let showDropdown = $state(false);
	let dropdownRef = $state<HTMLDivElement | null>(null);
	let groupedSelector = $state<ReturnType<typeof GroupedSelector> | null>(null);

	// Add project modal state
	let showAddModal = $state(false);

	// Get selected project name
	const selectedProjectName = $derived(
		$projects.find((p) => p.id === selectedProject)?.name || 'your project'
	);

	// Close dropdown on outside click
	function handleClickOutside(e: MouseEvent) {
		if (dropdownRef && !dropdownRef.contains(e.target as Node)) {
			showDropdown = false;
		}
	}

	function handleSelect(id: string) {
		onSelectProject(id);
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
		}
	}
</script>

<svelte:window onclick={handleClickOutside} />

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
			class="picker-trigger"
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
			<div class="picker-dropdown">
				<GroupedSelector
					bind:this={groupedSelector}
					items={$projects}
					entityType="projects"
					selectedId={selectedProject}
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
		padding: 2rem;
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
		border-radius: 12px;
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
	@media (prefers-reduced-motion: reduce) {
		.picker-dropdown {
			animation: none;
		}
	}
</style>
