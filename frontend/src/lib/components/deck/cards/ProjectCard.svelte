<script lang="ts">
	/**
	 * ProjectCard - Project management card for The Deck
	 *
	 * Features:
	 * - Project list with name, path display
	 * - Group badge display
	 * - Delete with inline confirmation
	 * - Create form that toggles in place
	 * - Search/filter for many projects
	 */
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import { tabs, projects } from '$lib/stores/tabs';
	import { groups } from '$lib/stores/groups';
	import { Search, Plus, Trash2, FolderOpen, X } from 'lucide-svelte';

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMinimize: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		mobile?: boolean;
	}

	let {
		card,
		onClose,
		onMinimize,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		mobile = false
	}: Props = $props();

	// Form state
	let showCreateForm = $state(false);
	let newProjectId = $state('');
	let newProjectName = $state('');
	let newProjectDescription = $state('');
	let creating = $state(false);
	let error = $state<string | null>(null);

	// Search state
	let searchQuery = $state('');

	// Delete confirmation state
	let deletingId = $state<string | null>(null);
	let deleteLoading = $state(false);

	// Filtered projects
	const filteredProjects = $derived(() => {
		if (!searchQuery.trim()) return $projects;
		const q = searchQuery.toLowerCase();
		return $projects.filter(
			p =>
				p.name.toLowerCase().includes(q) ||
				p.id.toLowerCase().includes(q) ||
				p.path.toLowerCase().includes(q)
		);
	});

	// Create project handler
	async function handleCreateProject() {
		if (!newProjectId.trim() || !newProjectName.trim()) return;

		creating = true;
		error = null;

		try {
			await tabs.createProject({
				id: newProjectId.trim(),
				name: newProjectName.trim(),
				description: newProjectDescription.trim() || undefined
			});
			// Reset form
			newProjectId = '';
			newProjectName = '';
			newProjectDescription = '';
			showCreateForm = false;
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to create project';
		} finally {
			creating = false;
		}
	}

	// Delete project handler
	async function handleDeleteProject() {
		if (!deletingId) return;

		deleteLoading = true;
		error = null;

		try {
			await tabs.deleteProject(deletingId);
			deletingId = null;
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to delete project';
		} finally {
			deleteLoading = false;
		}
	}

	// Cancel create form
	function cancelCreate() {
		showCreateForm = false;
		newProjectId = '';
		newProjectName = '';
		newProjectDescription = '';
		error = null;
	}
</script>

{#if mobile}
	<!-- Mobile: No BaseCard wrapper, just the content -->
	<div class="project-card-content mobile flex flex-col h-full bg-card">
		<!-- Header -->
		<div class="px-4 py-3 border-b border-border bg-gradient-to-r from-primary/5 to-transparent">
			<div class="flex items-center justify-between">
				<div>
					<h2 class="text-lg font-semibold text-foreground">Projects</h2>
					<p class="text-xs text-muted-foreground">
						{$projects.length} project{$projects.length !== 1 ? 's' : ''}
					</p>
				</div>
			</div>

			<!-- Search bar (show if > 3 projects) -->
			{#if $projects.length > 3}
				<div class="mt-3 relative">
					<Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
					<input
						type="text"
						bind:value={searchQuery}
						placeholder="Search projects..."
						class="w-full pl-10 pr-4 py-2 bg-background border border-border rounded-xl text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
					/>
				</div>
			{/if}
		</div>

		<!-- Content -->
		<div class="flex-1 overflow-y-auto p-4">
			{#if error}
				<div class="mb-4 p-3 bg-red-500/10 border border-red-500/20 rounded-xl text-red-500 text-sm flex items-center gap-2">
					<span class="flex-1">{error}</span>
					<button onclick={() => (error = null)} class="hover:text-red-400">
						<X class="w-4 h-4" />
					</button>
				</div>
			{/if}

			<!-- Delete confirmation banner -->
			{#if deletingId}
				<div class="mb-4 p-4 bg-red-500/10 border border-red-500/20 rounded-xl">
					<p class="text-sm text-foreground mb-3">
						Delete <strong>{$projects.find(p => p.id === deletingId)?.name}</strong>? This cannot be undone.
					</p>
					<div class="flex gap-2">
						<button
							onclick={() => (deletingId = null)}
							disabled={deleteLoading}
							class="flex-1 px-3 py-2 bg-muted text-foreground rounded-lg hover:bg-accent transition-colors text-sm"
						>
							Cancel
						</button>
						<button
							onclick={handleDeleteProject}
							disabled={deleteLoading}
							class="flex-1 px-3 py-2 bg-red-500 text-white rounded-lg hover:bg-red-600 transition-colors text-sm flex items-center justify-center gap-2"
						>
							{#if deleteLoading}
								<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
									<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
									<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
								</svg>
							{/if}
							Delete
						</button>
					</div>
				</div>
			{/if}

			<!-- Project list -->
			{#if filteredProjects().length === 0}
				<div class="text-center py-8">
					{#if searchQuery}
						<Search class="w-12 h-12 mx-auto text-muted-foreground/50 mb-3" />
						<p class="text-sm text-muted-foreground">No projects match "{searchQuery}"</p>
						<button onclick={() => (searchQuery = '')} class="mt-2 text-xs text-primary hover:underline">
							Clear search
						</button>
					{:else}
						<FolderOpen class="w-12 h-12 mx-auto text-muted-foreground/50 mb-3" />
						<p class="text-sm text-muted-foreground mb-2">No projects configured yet</p>
						<p class="text-xs text-muted-foreground/70 max-w-xs mx-auto">
							Projects organize your work into separate workspaces with their own settings.
						</p>
					{/if}
				</div>
			{:else}
				<div class="space-y-2">
					{#each filteredProjects() as project (project.id)}
						<div class="p-3 bg-background hover:bg-accent border border-border rounded-xl transition-all group">
							<div class="flex items-start gap-3">
								<div class="w-9 h-9 rounded-lg bg-primary/10 flex items-center justify-center flex-shrink-0">
									<FolderOpen class="w-4 h-4 text-primary" />
								</div>
								<div class="flex-1 min-w-0">
									<div class="flex items-center gap-2 flex-wrap">
										<span class="font-medium text-foreground text-sm">{project.name}</span>
										{#if $groups.projects.assignments[project.id]}
											<span class="text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary">
												{$groups.projects.assignments[project.id]}
											</span>
										{/if}
									</div>
									<p class="text-xs text-muted-foreground font-mono mt-0.5 truncate">/workspace/{project.path}/</p>
								</div>
								<button
									onclick={() => (deletingId = project.id)}
									class="p-2 text-muted-foreground hover:text-red-500 rounded-lg hover:bg-red-500/10 transition-colors opacity-0 group-hover:opacity-100"
									title="Delete project"
								>
									<Trash2 class="w-4 h-4" />
								</button>
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</div>

		<!-- Footer -->
		<div class="px-4 py-4 border-t border-border bg-muted/30">
			{#if showCreateForm}
				<div class="space-y-3">
					<div>
						<label class="block text-xs text-muted-foreground mb-1">ID</label>
						<input
							bind:value={newProjectId}
							class="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
							placeholder="my-project"
						/>
					</div>
					<div>
						<label class="block text-xs text-muted-foreground mb-1">Name</label>
						<input
							bind:value={newProjectName}
							class="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
							placeholder="My Project"
						/>
					</div>
					<div>
						<label class="block text-xs text-muted-foreground mb-1">Description</label>
						<textarea
							bind:value={newProjectDescription}
							class="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-foreground resize-none focus:outline-none focus:ring-2 focus:ring-primary/50"
							rows="2"
							placeholder="Optional"
						></textarea>
					</div>
					<div class="flex gap-2">
						<button
							onclick={cancelCreate}
							disabled={creating}
							class="flex-1 px-4 py-2.5 bg-muted text-foreground rounded-xl hover:bg-accent transition-colors"
						>
							Cancel
						</button>
						<button
							onclick={handleCreateProject}
							disabled={creating || !newProjectId.trim() || !newProjectName.trim()}
							class="flex-1 px-4 py-2.5 bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
						>
							{#if creating}
								<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
									<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
									<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
								</svg>
							{/if}
							Create
						</button>
					</div>
				</div>
			{:else}
				<button
					onclick={() => (showCreateForm = true)}
					class="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-colors font-medium"
				>
					<Plus class="w-4 h-4" />
					New Project
				</button>
			{/if}
		</div>
	</div>
{:else}
	<!-- Desktop: Wrap in BaseCard -->
	<BaseCard
		{card}
		{onClose}
		{onMinimize}
		{onMaximize}
		{onFocus}
		{onMove}
		{onResize}
		{onDragEnd}
		{onResizeEnd}
	>
		<div class="project-card-content flex flex-col h-full">
			<!-- Header -->
			<div class="px-4 py-3 border-b border-border bg-gradient-to-r from-primary/5 to-transparent">
				<div class="flex items-center justify-between">
					<div>
						<p class="text-xs text-muted-foreground">
							{$projects.length} project{$projects.length !== 1 ? 's' : ''} configured
						</p>
					</div>
				</div>

				<!-- Search bar (show if > 3 projects) -->
				{#if $projects.length > 3}
					<div class="mt-3 relative">
						<Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
						<input
							type="text"
							bind:value={searchQuery}
							placeholder="Search projects..."
							class="w-full pl-10 pr-4 py-2 bg-background border border-border rounded-xl text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
						/>
					</div>
				{/if}
			</div>

			<!-- Content -->
			<div class="flex-1 overflow-y-auto p-4">
				{#if error}
					<div class="mb-4 p-3 bg-red-500/10 border border-red-500/20 rounded-xl text-red-500 text-sm flex items-center gap-2">
						<span class="flex-1">{error}</span>
						<button onclick={() => (error = null)} class="hover:text-red-400">
							<X class="w-4 h-4" />
						</button>
					</div>
				{/if}

				<!-- Delete confirmation banner -->
				{#if deletingId}
					<div class="mb-4 p-4 bg-red-500/10 border border-red-500/20 rounded-xl">
						<p class="text-sm text-foreground mb-3">
							Delete <strong>{$projects.find(p => p.id === deletingId)?.name}</strong>? This cannot be undone.
						</p>
						<div class="flex gap-2">
							<button
								onclick={() => (deletingId = null)}
								disabled={deleteLoading}
								class="flex-1 px-3 py-2 bg-muted text-foreground rounded-lg hover:bg-accent transition-colors text-sm"
							>
								Cancel
							</button>
							<button
								onclick={handleDeleteProject}
								disabled={deleteLoading}
								class="flex-1 px-3 py-2 bg-red-500 text-white rounded-lg hover:bg-red-600 transition-colors text-sm flex items-center justify-center gap-2"
							>
								{#if deleteLoading}
									<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
										<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
										<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
									</svg>
								{/if}
								Delete
							</button>
						</div>
					</div>
				{/if}

				<!-- Project list -->
				{#if filteredProjects().length === 0}
					<div class="text-center py-8">
						{#if searchQuery}
							<Search class="w-12 h-12 mx-auto text-muted-foreground/50 mb-3" />
							<p class="text-sm text-muted-foreground">No projects match "{searchQuery}"</p>
							<button onclick={() => (searchQuery = '')} class="mt-2 text-xs text-primary hover:underline">
								Clear search
							</button>
						{:else}
							<FolderOpen class="w-12 h-12 mx-auto text-muted-foreground/50 mb-3" />
							<p class="text-sm text-muted-foreground mb-2">No projects configured yet</p>
							<p class="text-xs text-muted-foreground/70 max-w-xs mx-auto">
								Projects organize your work into separate workspaces with their own settings.
							</p>
						{/if}
					</div>
				{:else}
					<div class="space-y-2">
						{#each filteredProjects() as project (project.id)}
							<div class="p-3 bg-background hover:bg-accent border border-border rounded-xl transition-all group">
								<div class="flex items-start gap-3">
									<div class="w-9 h-9 rounded-lg bg-primary/10 flex items-center justify-center flex-shrink-0">
										<FolderOpen class="w-4 h-4 text-primary" />
									</div>
									<div class="flex-1 min-w-0">
										<div class="flex items-center gap-2 flex-wrap">
											<span class="font-medium text-foreground text-sm">{project.name}</span>
											{#if $groups.projects.assignments[project.id]}
												<span class="text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary">
													{$groups.projects.assignments[project.id]}
												</span>
											{/if}
										</div>
										<p class="text-xs text-muted-foreground font-mono mt-0.5 truncate">/workspace/{project.path}/</p>
									</div>
									<button
										onclick={() => (deletingId = project.id)}
										class="p-2 text-muted-foreground hover:text-red-500 rounded-lg hover:bg-red-500/10 transition-colors opacity-0 group-hover:opacity-100"
										title="Delete project"
									>
										<Trash2 class="w-4 h-4" />
									</button>
								</div>
							</div>
						{/each}
					</div>
				{/if}
			</div>

			<!-- Footer -->
			<div class="px-4 py-4 border-t border-border bg-muted/30">
				{#if showCreateForm}
					<div class="space-y-3">
						<div>
							<label class="block text-xs text-muted-foreground mb-1">ID</label>
							<input
								bind:value={newProjectId}
								class="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
								placeholder="my-project"
							/>
						</div>
						<div>
							<label class="block text-xs text-muted-foreground mb-1">Name</label>
							<input
								bind:value={newProjectName}
								class="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
								placeholder="My Project"
							/>
						</div>
						<div>
							<label class="block text-xs text-muted-foreground mb-1">Description</label>
							<textarea
								bind:value={newProjectDescription}
								class="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-foreground resize-none focus:outline-none focus:ring-2 focus:ring-primary/50"
								rows="2"
								placeholder="Optional"
							></textarea>
						</div>
						<div class="flex gap-2">
							<button
								onclick={cancelCreate}
								disabled={creating}
								class="flex-1 px-4 py-2.5 bg-muted text-foreground rounded-xl hover:bg-accent transition-colors"
							>
								Cancel
							</button>
							<button
								onclick={handleCreateProject}
								disabled={creating || !newProjectId.trim() || !newProjectName.trim()}
								class="flex-1 px-4 py-2.5 bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
							>
								{#if creating}
									<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
										<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
										<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
									</svg>
								{/if}
								Create
							</button>
						</div>
					</div>
				{:else}
					<button
						onclick={() => (showCreateForm = true)}
						class="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-colors font-medium"
					>
						<Plus class="w-4 h-4" />
						New Project
					</button>
				{/if}
			</div>
		</div>
	</BaseCard>
{/if}

<style>
	.project-card-content {
		background: var(--card);
	}

	.project-card-content.mobile {
		border-radius: 0;
	}
</style>
