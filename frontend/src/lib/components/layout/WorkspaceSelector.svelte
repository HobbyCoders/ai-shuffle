<script lang="ts">
	/**
	 * WorkspaceSelector - "Let's build [workspace]" empty state
	 *
	 * Codex-style workspace picker shown when starting a new chat.
	 * Centered hero with clickable workspace name dropdown.
	 * Includes unified "Add Project" modal with directory browser.
	 */
	import { FolderClosed, ChevronDown, FolderPlus, Check } from 'lucide-svelte';
	import { projects } from '$lib/stores/tabs';
	import AddProjectModal from '$lib/components/AddProjectModal.svelte';

	interface Props {
		selectedProject: string | null;
		onSelectProject: (projectId: string) => void;
	}

	let { selectedProject, onSelectProject }: Props = $props();

	let showDropdown = $state(false);
	let dropdownRef = $state<HTMLDivElement | null>(null);

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
			onclick={() => showDropdown = !showDropdown}
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
				<p class="dropdown-label">Select your workspace</p>
				<div class="dropdown-scroll">
					{#each $projects as project (project.id)}
						<button
							class="dropdown-item"
							class:selected={selectedProject === project.id}
							onclick={() => handleSelect(project.id)}
						>
							<FolderClosed size={16} strokeWidth={1.5} />
							<span class="item-name">{project.name}</span>
							{#if selectedProject === project.id}
								<Check size={16} strokeWidth={2} class="check-icon" />
							{/if}
						</button>
					{:else}
						<p class="dropdown-empty">No workspaces available</p>
					{/each}
				</div>
				<div class="dropdown-divider"></div>
				<button class="dropdown-item new-project-item" onclick={openAddModal}>
					<FolderPlus size={16} strokeWidth={1.5} />
					<span class="item-name">Add Project</span>
				</button>
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

	.dropdown-scroll {
		max-height: min(40vh, 320px);
		overflow-y: auto;
		overscroll-behavior: contain;
	}

	.dropdown-label {
		padding: 8px 12px 4px;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		font-weight: 500;
	}

	.dropdown-item {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 10px 12px;
		background: transparent;
		border: none;
		border-radius: 8px;
		color: var(--foreground);
		font-size: 0.875rem;
		cursor: pointer;
		text-align: left;
		transition: background-color 120ms ease;
	}

	.dropdown-item:hover {
		background: var(--hover-overlay);
	}

	.dropdown-item.selected {
		background: color-mix(in oklch, var(--primary) 8%, transparent);
	}

	.dropdown-item :global(svg) {
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.dropdown-item.selected :global(svg) {
		color: var(--foreground);
	}

	.item-name {
		flex: 1;
	}

	.dropdown-item :global(.check-icon) {
		color: var(--primary);
	}

	.dropdown-empty {
		padding: 12px;
		text-align: center;
		color: var(--muted-foreground);
		font-size: 0.8125rem;
	}

	.dropdown-divider {
		height: 1px;
		background: var(--border);
		margin: 4px 0;
	}

	.new-project-item {
		color: var(--primary) !important;
	}

	.new-project-item :global(svg) {
		color: var(--primary) !important;
	}

	/* Reduced motion */
	@media (prefers-reduced-motion: reduce) {
		.picker-dropdown {
			animation: none;
		}
	}

</style>
