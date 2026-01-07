<script lang="ts">
	/**
	 * SubagentList - Displays and manages subagents for a profile
	 *
	 * Responsive design for mobile and desktop. Supports:
	 * - Listing all subagents
	 * - Creating new subagents
	 * - Editing existing subagents
	 * - Deleting subagents
	 */
	import { api } from '$lib/api/client';
	import SubagentEditorV2 from './SubagentEditorV2.svelte';

	interface Subagent {
		name: string;
		description: string;
		prompt: string;
		tools?: string[];
		model?: string;
	}

	interface Props {
		profileId: string;
		onClose?: () => void;
	}

	let { profileId, onClose }: Props = $props();

	let subagents = $state<Subagent[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Editor modal state
	let showEditor = $state(false);
	let editingSubagent = $state<Subagent | null>(null);

	// Delete confirmation state
	let deletingAgent = $state<string | null>(null);
	let deleteLoading = $state(false);

	// Load subagents
	async function loadSubagents() {
		loading = true;
		error = null;

		try {
			subagents = await api.get<Subagent[]>(`/profiles/${profileId}/agents`);
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to load subagents';
		} finally {
			loading = false;
		}
	}

	// Initial load
	$effect(() => {
		loadSubagents();
	});

	// Open editor for new subagent
	function handleCreate() {
		editingSubagent = null;
		showEditor = true;
	}

	// Open editor for existing subagent
	function handleEdit(agent: Subagent) {
		editingSubagent = agent;
		showEditor = true;
	}

	// Handle delete confirmation
	function handleDeleteClick(agentName: string) {
		deletingAgent = agentName;
	}

	function cancelDelete() {
		deletingAgent = null;
	}

	async function confirmDelete() {
		if (!deletingAgent) return;

		deleteLoading = true;
		try {
			await api.delete(`/profiles/${profileId}/agents/${deletingAgent}`);
			deletingAgent = null;
			await loadSubagents();
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to delete subagent';
		} finally {
			deleteLoading = false;
		}
	}

	// Close editor and refresh
	function handleEditorClose() {
		showEditor = false;
		editingSubagent = null;
	}

	function handleEditorSave() {
		loadSubagents();
	}

	// Model display name
	function getModelDisplay(model?: string): string {
		switch (model) {
			case 'haiku': return 'Haiku';
			case 'sonnet': return 'Sonnet';
			case 'sonnet-1m': return 'Sonnet 1M';
			case 'opus': return 'Opus';
			default: return 'Inherited';
		}
	}
</script>

<div class="flex flex-col h-full">
	<!-- Header -->
	<div class="flex items-center justify-between px-4 py-3 border-b border-border">
		<div class="flex items-center gap-2">
			{#if onClose}
				<button
					onclick={onClose}
					class="p-1 hover:bg-muted rounded-md transition-colors md:hidden"
					aria-label="Back"
				>
					<svg class="w-5 h-5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
					</svg>
				</button>
			{/if}
			<h2 class="text-lg font-semibold text-foreground">Subagents</h2>
			<span class="text-sm text-muted-foreground">({subagents.length})</span>
		</div>
		<button
			onclick={handleCreate}
			class="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-primary-foreground bg-primary rounded-md hover:bg-primary/90 transition-colors"
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
			</svg>
			<span class="hidden sm:inline">Add Agent</span>
		</button>
	</div>

	<!-- Content -->
	<div class="flex-1 overflow-y-auto p-4">
		{#if error}
			<div class="p-3 bg-red-500/10 border border-red-500/20 rounded-md text-red-500 text-sm mb-4">
				{error}
				<button onclick={() => error = null} class="ml-2 underline">Dismiss</button>
			</div>
		{/if}

		{#if loading}
			<div class="flex items-center justify-center py-8">
				<svg class="w-6 h-6 animate-spin text-primary" fill="none" viewBox="0 0 24 24">
					<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
					<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
				</svg>
			</div>
		{:else if subagents.length === 0}
			<div class="text-center py-8">
				<div class="w-12 h-12 mx-auto mb-3 rounded-full bg-muted flex items-center justify-center">
					<svg class="w-6 h-6 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
					</svg>
				</div>
				<p class="text-muted-foreground mb-4">No subagents configured</p>
				<button
					onclick={handleCreate}
					class="text-primary hover:underline"
				>
					Create your first subagent
				</button>
			</div>
		{:else}
			<div class="space-y-3">
				{#each subagents as agent (agent.name)}
					<div class="border border-border rounded-lg overflow-hidden bg-card hover:border-primary/50 transition-colors">
						<!-- Agent header -->
						<div class="px-4 py-3">
							<div class="flex items-start justify-between gap-2">
								<div class="flex-1 min-w-0">
									<div class="flex items-center gap-2 flex-wrap">
										<h3 class="font-medium text-foreground">{agent.name}</h3>
										<span class="px-1.5 py-0.5 text-xs font-medium bg-primary/10 text-primary rounded">
											{getModelDisplay(agent.model)}
										</span>
									</div>
									<p class="text-sm text-muted-foreground mt-0.5 line-clamp-2">
										{agent.description}
									</p>
								</div>

								<!-- Actions -->
								<div class="flex items-center gap-1">
									<button
										onclick={() => handleEdit(agent)}
										class="p-1.5 hover:bg-muted rounded-md transition-colors"
										aria-label="Edit"
									>
										<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
										</svg>
									</button>
									<button
										onclick={() => handleDeleteClick(agent.name)}
										class="p-1.5 hover:bg-red-500/10 rounded-md transition-colors"
										aria-label="Delete"
									>
										<svg class="w-4 h-4 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
										</svg>
									</button>
								</div>
							</div>

							<!-- Tools -->
							{#if agent.tools && agent.tools.length > 0}
								<div class="flex flex-wrap gap-1 mt-2">
									{#each agent.tools as tool}
										<span class="px-1.5 py-0.5 text-xs bg-muted text-muted-foreground rounded">
											{tool}
										</span>
									{/each}
								</div>
							{:else}
								<p class="text-xs text-muted-foreground mt-2">All tools available</p>
							{/if}
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</div>
</div>

<!-- Delete confirmation modal -->
{#if deletingAgent}
	<div
		class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"
		onclick={(e) => e.target === e.currentTarget && cancelDelete()}
		role="dialog"
		aria-modal="true"
	>
		<div class="bg-card border border-border rounded-lg shadow-xl w-full max-w-sm p-4">
			<h3 class="text-lg font-semibold text-foreground mb-2">Delete Subagent</h3>
			<p class="text-muted-foreground mb-4">
				Are you sure you want to delete <strong>{deletingAgent}</strong>? This action cannot be undone.
			</p>
			<div class="flex justify-end gap-2">
				<button
					onclick={cancelDelete}
					disabled={deleteLoading}
					class="px-3 py-1.5 text-sm font-medium text-foreground bg-background border border-border rounded-md hover:bg-muted transition-colors"
				>
					Cancel
				</button>
				<button
					onclick={confirmDelete}
					disabled={deleteLoading}
					class="px-3 py-1.5 text-sm font-medium text-white bg-red-500 rounded-md hover:bg-red-600 transition-colors disabled:opacity-50 flex items-center gap-2"
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
	</div>
{/if}

<!-- Editor modal -->
{#if showEditor}
	<SubagentEditorV2
		{profileId}
		subagent={editingSubagent}
		isGlobal={false}
		onClose={handleEditorClose}
		onSave={handleEditorSave}
	/>
{/if}
