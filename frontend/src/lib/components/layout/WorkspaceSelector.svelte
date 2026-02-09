<script lang="ts">
	/**
	 * WorkspaceSelector - "Let's build [workspace]" empty state
	 *
	 * Codex-style workspace picker shown when starting a new chat.
	 * Centered hero with clickable workspace name dropdown.
	 * Includes "New Project" button that opens a small creation modal.
	 */
	import { FolderClosed, ChevronDown, Plus, Check, X } from 'lucide-svelte';
	import { projects, tabs } from '$lib/stores/tabs';

	interface Props {
		selectedProject: string | null;
		onSelectProject: (projectId: string) => void;
	}

	let { selectedProject, onSelectProject }: Props = $props();

	let showDropdown = $state(false);
	let dropdownRef = $state<HTMLDivElement | null>(null);

	// New project modal state
	let showCreateModal = $state(false);
	let newProjectId = $state('');
	let newProjectName = $state('');
	let creating = $state(false);
	let createError = $state<string | null>(null);

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

	function handleSelect(projectId: string) {
		onSelectProject(projectId);
		showDropdown = false;
	}

	function openCreateModal() {
		showDropdown = false;
		showCreateModal = true;
		newProjectId = '';
		newProjectName = '';
		createError = null;
	}

	async function handleCreateProject() {
		if (!newProjectId.trim() || !newProjectName.trim()) return;

		creating = true;
		createError = null;

		try {
			await tabs.createProject({
				id: newProjectId.trim(),
				name: newProjectName.trim()
			});
			showCreateModal = false;
			// Auto-select the newly created project
			onSelectProject(newProjectId.trim());
			newProjectId = '';
			newProjectName = '';
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			createError = err.detail || err.message || 'Failed to create project';
		} finally {
			creating = false;
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
				<div class="dropdown-divider"></div>
				<button class="dropdown-item new-project-item" onclick={openCreateModal}>
					<Plus size={16} strokeWidth={1.5} />
					<span class="item-name">New Project</span>
				</button>
			</div>
		{/if}
	</div>
</div>

<!-- New Project Modal -->
{#if showCreateModal}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<div class="create-modal-backdrop" onclick={() => showCreateModal = false}>
		<div class="create-modal" onclick={(e) => e.stopPropagation()}>
			<div class="create-modal-header">
				<h3 class="create-modal-title">New Project</h3>
				<button class="create-modal-close" onclick={() => showCreateModal = false}>
					<X size={16} strokeWidth={1.5} />
				</button>
			</div>

			{#if createError}
				<div class="create-modal-error">
					{createError}
				</div>
			{/if}

			<div class="create-modal-field">
				<label class="create-modal-label">Project ID</label>
				<input
					type="text"
					class="create-modal-input"
					placeholder="my-project"
					bind:value={newProjectId}
					onkeydown={(e) => { if (e.key === 'Enter' && newProjectId.trim() && newProjectName.trim()) handleCreateProject(); }}
				/>
			</div>

			<div class="create-modal-field">
				<label class="create-modal-label">Display Name</label>
				<input
					type="text"
					class="create-modal-input"
					placeholder="My Project"
					bind:value={newProjectName}
					onkeydown={(e) => { if (e.key === 'Enter' && newProjectId.trim() && newProjectName.trim()) handleCreateProject(); }}
				/>
			</div>

			<div class="create-modal-actions">
				<button class="modal-btn cancel" onclick={() => showCreateModal = false}>
					Cancel
				</button>
				<button
					class="modal-btn create"
					disabled={!newProjectId.trim() || !newProjectName.trim() || creating}
					onclick={handleCreateProject}
				>
					{#if creating}Creating...{:else}Create{/if}
				</button>
			</div>
		</div>
	</div>
{/if}

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

	/* Create Project Modal */
	.create-modal-backdrop {
		position: fixed;
		inset: 0;
		background: color-mix(in oklch, var(--background) 70%, transparent);
		backdrop-filter: blur(4px);
		display: flex;
		align-items: center;
		justify-content: center;
		z-index: 1000;
	}

	.create-modal {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 12px;
		padding: 20px;
		width: 380px;
		max-width: 90vw;
		box-shadow: var(--shadow-l);
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.create-modal-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.create-modal-title {
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0;
	}

	.create-modal-close {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
	}

	.create-modal-close:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.create-modal-error {
		padding: 8px 12px;
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--destructive) 25%, transparent);
		border-radius: 8px;
		color: var(--destructive);
		font-size: 0.8125rem;
	}

	.create-modal-field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.create-modal-label {
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--muted-foreground);
	}

	.create-modal-input {
		width: 100%;
		padding: 8px 12px;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 8px;
		color: var(--foreground);
		font-size: 0.8125rem;
		outline: none;
		transition: border-color 150ms ease;
	}

	.create-modal-input:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	.create-modal-actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		margin-top: 4px;
	}

	.modal-btn {
		padding: 6px 14px;
		font-size: 0.8125rem;
		font-weight: 500;
		border-radius: 8px;
		border: none;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.modal-btn.cancel {
		background: var(--secondary);
		color: var(--muted-foreground);
	}

	.modal-btn.cancel:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.modal-btn.create {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.modal-btn.create:hover {
		opacity: 0.9;
	}

	.modal-btn.create:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	/* Reduced motion */
	@media (prefers-reduced-motion: reduce) {
		.picker-dropdown {
			animation: none;
		}
	}
</style>
