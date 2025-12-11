<script lang="ts">
	/**
	 * SubagentManager - Global subagent management panel
	 *
	 * Displays all subagents in a clean list with CRUD operations.
	 * Uses SubagentEditorV2 for the actual edit/create modal.
	 */
	import { api } from '$lib/api/client';
	import { groups } from '$lib/stores/groups';
	import SubagentEditorV2 from './SubagentEditorV2.svelte';

	interface Subagent {
		id: string;
		name: string;
		description: string;
		prompt: string;
		tools?: string[];
		model?: string;
		is_builtin: boolean;
		created_at: string;
		updated_at: string;
	}

	interface Props {
		onClose: () => void;
		onUpdate?: () => void;
	}

	let { onClose, onUpdate }: Props = $props();

	// State
	let subagents = $state<Subagent[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Editor state
	let showEditor = $state(false);
	let editingSubagent = $state<Subagent | null>(null);

	// Delete state
	let deletingId = $state<string | null>(null);
	let deleteLoading = $state(false);

	// Import state
	let importInput: HTMLInputElement;
	let importing = $state(false);

	// Search/filter
	let searchQuery = $state('');

	// Filtered subagents
	const filteredSubagents = $derived(() => {
		if (!searchQuery.trim()) return subagents;
		const q = searchQuery.toLowerCase();
		return subagents.filter(
			s =>
				s.name.toLowerCase().includes(q) ||
				s.id.toLowerCase().includes(q) ||
				s.description.toLowerCase().includes(q)
		);
	});

	// Load subagents
	async function loadSubagents() {
		loading = true;
		error = null;
		try {
			subagents = await api.get<Subagent[]>('/subagents');
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to load subagents';
		} finally {
			loading = false;
		}
	}

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

	// Handle editor save
	async function handleEditorSave() {
		showEditor = false;
		editingSubagent = null;
		await loadSubagents();
		onUpdate?.();
	}

	// Delete subagent
	function handleDeleteClick(id: string, e: Event) {
		e.stopPropagation();
		deletingId = id;
	}

	async function confirmDelete() {
		if (!deletingId) return;
		deleteLoading = true;
		try {
			await api.delete(`/subagents/${deletingId}`);
			deletingId = null;
			await loadSubagents();
			onUpdate?.();
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to delete subagent';
		} finally {
			deleteLoading = false;
		}
	}

	function getModelDisplay(model?: string): string {
		switch (model) {
			case 'haiku':
				return 'Haiku';
			case 'sonnet':
				return 'Sonnet';
			case 'sonnet-1m':
				return 'Sonnet 1M';
			case 'opus':
				return 'Opus';
			default:
				return 'Inherit';
		}
	}

	function getModelColor(model?: string): string {
		switch (model) {
			case 'haiku':
				return 'bg-emerald-500/10 text-emerald-500';
			case 'sonnet':
				return 'bg-blue-500/10 text-blue-500';
			case 'sonnet-1m':
				return 'bg-violet-500/10 text-violet-500';
			case 'opus':
				return 'bg-amber-500/10 text-amber-500';
			default:
				return 'bg-muted text-muted-foreground';
		}
	}

	// Export a single subagent
	async function exportSubagent(agentId: string, e: Event) {
		e.stopPropagation();
		try {
			const data = await api.get<any>(`/export-import/subagents/${agentId}`);
			const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `subagent-${agentId}-${new Date().toISOString().split('T')[0]}.json`;
			document.body.appendChild(a);
			a.click();
			document.body.removeChild(a);
			URL.revokeObjectURL(url);
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = 'Export failed: ' + (err.detail || err.message || 'Unknown error');
		}
	}

	// Trigger import file dialog
	function triggerImport() {
		importInput?.click();
	}

	// Handle import file selection
	async function handleImport(event: Event) {
		const input = event.target as HTMLInputElement;
		const file = input.files?.[0];
		if (!file) return;
		input.value = '';

		importing = true;
		error = null;

		try {
			const content = await file.text();
			const data = JSON.parse(content);

			if (!data.subagents || data.subagents.length === 0) {
				error = 'No subagents found in file';
				importing = false;
				return;
			}

			const result = await api.post<any>('/export-import/import/json?overwrite_existing=false', data);

			if (result.subagents_imported > 0) {
				await loadSubagents();
				onUpdate?.();
			} else if (result.subagents_skipped > 0) {
				error = 'Subagent already exists. Use a different ID or delete the existing one first.';
			} else if (result.errors?.length > 0) {
				error = 'Import failed: ' + result.errors.join(', ');
			}
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = 'Import failed: ' + (err.detail || err.message || 'Invalid JSON file');
		}

		importing = false;
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			if (showEditor) {
				showEditor = false;
			} else if (deletingId) {
				deletingId = null;
			} else {
				onClose();
			}
		}
	}
</script>

<svelte:window on:keydown={handleKeydown} />

<!-- Hidden file input for import -->
<input type="file" accept=".json" bind:this={importInput} onchange={handleImport} class="hidden" />

<!-- Modal backdrop -->
<div
	class="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-2 sm:p-4"
	onclick={() => onClose()}
	role="dialog"
	aria-modal="true"
>
	<!-- Modal content -->
	<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
	<div
		class="bg-card rounded-2xl w-full max-w-lg max-h-[95vh] sm:max-h-[85vh] overflow-hidden shadow-2xl border border-border flex flex-col"
		onclick={(e) => e.stopPropagation()}
	>
		<!-- Header -->
		<div class="px-4 sm:px-6 py-4 border-b border-border bg-gradient-to-r from-primary/5 to-transparent">
			<div class="flex items-center justify-between">
				<div>
					<h2 class="text-lg font-semibold text-foreground">Subagents</h2>
					<p class="text-xs text-muted-foreground mt-0.5">
						{subagents.length} agent{subagents.length !== 1 ? 's' : ''} configured
					</p>
				</div>
				<button class="p-2 hover:bg-muted rounded-lg transition-colors" onclick={onClose}>
					<svg class="w-5 h-5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<!-- Search bar (only show if > 3 agents) -->
			{#if subagents.length > 3}
				<div class="mt-3 relative">
					<svg
						class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground"
						fill="none"
						stroke="currentColor"
						viewBox="0 0 24 24"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="2"
							d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
						/>
					</svg>
					<input
						type="text"
						bind:value={searchQuery}
						placeholder="Search subagents..."
						class="w-full pl-10 pr-4 py-2 bg-background border border-border rounded-xl text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
					/>
				</div>
			{/if}
		</div>

		<!-- Content -->
		<div class="flex-1 overflow-y-auto p-4 sm:p-6">
			{#if error}
				<div
					class="mb-4 p-3 bg-red-500/10 border border-red-500/20 rounded-xl text-red-500 text-sm flex items-center gap-2"
				>
					<svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="2"
							d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
						/>
					</svg>
					<span class="flex-1">{error}</span>
					<button onclick={() => (error = null)} class="hover:text-red-400">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>
			{/if}

			{#if loading}
				<div class="flex items-center justify-center py-12">
					<svg class="w-8 h-8 animate-spin text-primary" fill="none" viewBox="0 0 24 24">
						<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
						<path
							class="opacity-75"
							fill="currentColor"
							d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
						></path>
					</svg>
				</div>
			{:else if filteredSubagents().length === 0}
				{#if searchQuery}
					<div class="text-center py-8">
						<svg
							class="w-12 h-12 mx-auto text-muted-foreground/50 mb-3"
							fill="none"
							stroke="currentColor"
							viewBox="0 0 24 24"
						>
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="1.5"
								d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
							/>
						</svg>
						<p class="text-sm text-muted-foreground">No subagents match "{searchQuery}"</p>
						<button onclick={() => (searchQuery = '')} class="mt-2 text-xs text-primary hover:underline">
							Clear search
						</button>
					</div>
				{:else}
					<div class="text-center py-8">
						<svg
							class="w-12 h-12 mx-auto text-muted-foreground/50 mb-3"
							fill="none"
							stroke="currentColor"
							viewBox="0 0 24 24"
						>
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="1.5"
								d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z"
							/>
						</svg>
						<p class="text-sm text-muted-foreground mb-4">No subagents configured yet</p>
						<p class="text-xs text-muted-foreground/70 max-w-xs mx-auto">
							Subagents are specialized AI assistants that can be delegated to for specific tasks.
						</p>
					</div>
				{/if}
			{:else}
				<div class="space-y-2">
					{#each filteredSubagents() as agent (agent.id)}
						<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
						<div
							class="p-4 bg-background hover:bg-accent border border-border rounded-xl transition-all group cursor-pointer"
							onclick={() => handleEdit(agent)}
						>
							<div class="flex items-start gap-3">
								<!-- Agent icon -->
								<div
									class="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center flex-shrink-0"
								>
									<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path
											stroke-linecap="round"
											stroke-linejoin="round"
											stroke-width="2"
											d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"
										/>
									</svg>
								</div>

								<!-- Agent info -->
								<div class="flex-1 min-w-0">
									<div class="flex items-center gap-2 flex-wrap">
										<span class="font-medium text-foreground">{agent.name}</span>
										<span class="text-[10px] px-1.5 py-0.5 rounded {getModelColor(agent.model)}">
											{getModelDisplay(agent.model)}
										</span>
										{#if agent.tools && agent.tools.length > 0}
											<span class="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
												{agent.tools.length} tools
											</span>
										{/if}
										{#if $groups.subagents.assignments[agent.id]}
											<span class="text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary">
												{$groups.subagents.assignments[agent.id]}
											</span>
										{/if}
									</div>
									<p class="text-xs text-muted-foreground font-mono mt-0.5">{agent.id}</p>
									<p class="text-xs text-muted-foreground mt-1 line-clamp-2">{agent.description}</p>
								</div>

								<!-- Actions -->
								<div class="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
									<!-- Group dropdown -->
									<div class="relative group/dropdown">
										<button
											onclick={(e) => e.stopPropagation()}
											class="p-2 text-muted-foreground hover:text-foreground rounded-lg hover:bg-muted transition-colors"
											title="Assign to group"
										>
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path
													stroke-linecap="round"
													stroke-linejoin="round"
													stroke-width="2"
													d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z"
												/>
											</svg>
										</button>
										<div
											class="absolute right-0 top-full mt-1 w-40 bg-card border border-border rounded-xl shadow-lg opacity-0 invisible group-hover/dropdown:opacity-100 group-hover/dropdown:visible transition-all z-50"
										>
											<div class="py-1 max-h-48 overflow-y-auto">
												{#each $groups.subagents.groups as group}
													<button
														onclick={(e) => {
															e.stopPropagation();
															groups.assignToGroup('subagents', agent.id, group.name);
														}}
														class="w-full px-3 py-2 text-left text-sm hover:bg-accent transition-colors flex items-center gap-2"
													>
														{#if $groups.subagents.assignments[agent.id] === group.name}
															<svg
																class="w-3 h-3 text-primary"
																fill="none"
																stroke="currentColor"
																viewBox="0 0 24 24"
															>
																<path
																	stroke-linecap="round"
																	stroke-linejoin="round"
																	stroke-width="2"
																	d="M5 13l4 4L19 7"
																/>
															</svg>
														{:else}
															<span class="w-3"></span>
														{/if}
														{group.name}
													</button>
												{/each}
												{#if $groups.subagents.assignments[agent.id]}
													<button
														onclick={(e) => {
															e.stopPropagation();
															groups.removeFromGroup('subagents', agent.id);
														}}
														class="w-full px-3 py-2 text-left text-sm hover:bg-accent transition-colors text-muted-foreground"
													>
														<span class="ml-5">Remove from group</span>
													</button>
												{/if}
												<div class="border-t border-border my-1"></div>
												<button
													onclick={(e) => {
														e.stopPropagation();
														const name = prompt('New group name:');
														if (name?.trim()) {
															groups.createGroup('subagents', name.trim());
															groups.assignToGroup('subagents', agent.id, name.trim());
														}
													}}
													class="w-full px-3 py-2 text-left text-sm hover:bg-accent transition-colors text-muted-foreground"
												>
													<span class="ml-5">+ New group</span>
												</button>
											</div>
										</div>
									</div>

									<!-- Export -->
									<button
										onclick={(e) => exportSubagent(agent.id, e)}
										class="p-2 text-muted-foreground hover:text-foreground rounded-lg hover:bg-muted transition-colors"
										title="Export"
									>
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path
												stroke-linecap="round"
												stroke-linejoin="round"
												stroke-width="2"
												d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"
											/>
										</svg>
									</button>

									<!-- Delete -->
									<button
										onclick={(e) => handleDeleteClick(agent.id, e)}
										class="p-2 text-muted-foreground hover:text-red-500 rounded-lg hover:bg-red-500/10 transition-colors"
										title="Delete"
									>
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path
												stroke-linecap="round"
												stroke-linejoin="round"
												stroke-width="2"
												d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
											/>
										</svg>
									</button>
								</div>
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</div>

		<!-- Footer -->
		<div class="px-4 sm:px-6 py-4 border-t border-border bg-muted/30">
			<div class="flex gap-2">
				<button
					onclick={handleCreate}
					class="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-colors font-medium"
				>
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
					</svg>
					New Subagent
				</button>
				<button
					onclick={triggerImport}
					disabled={importing}
					class="px-4 py-2.5 bg-background border border-border text-foreground rounded-xl hover:bg-muted transition-colors flex items-center gap-2"
					title="Import from file"
				>
					{#if importing}
						<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"
							></circle>
							<path
								class="opacity-75"
								fill="currentColor"
								d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
							></path>
						</svg>
					{:else}
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="2"
								d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"
							/>
						</svg>
					{/if}
					<span class="hidden sm:inline">Import</span>
				</button>
			</div>
		</div>
	</div>
</div>

<!-- Editor Modal -->
{#if showEditor}
	<SubagentEditorV2
		isGlobal={true}
		subagent={editingSubagent}
		onClose={() => {
			showEditor = false;
			editingSubagent = null;
		}}
		onSave={handleEditorSave}
	/>
{/if}

<!-- Delete Confirmation -->
{#if deletingId}
	<div
		class="fixed inset-0 bg-black/60 backdrop-blur-sm z-[70] flex items-center justify-center p-4"
		onclick={() => (deletingId = null)}
		role="dialog"
		aria-modal="true"
	>
		<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
		<div
			class="bg-card rounded-2xl w-full max-w-sm shadow-2xl border border-border"
			onclick={(e) => e.stopPropagation()}
		>
			<div class="p-6 text-center">
				<div
					class="w-12 h-12 rounded-full bg-red-500/10 flex items-center justify-center mx-auto mb-4"
				>
					<svg class="w-6 h-6 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="2"
							d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
						/>
					</svg>
				</div>
				<h3 class="text-lg font-semibold text-foreground mb-2">Delete Subagent</h3>
				<p class="text-sm text-muted-foreground mb-6">
					Are you sure you want to delete <strong class="text-foreground"
						>{subagents.find((s) => s.id === deletingId)?.name}</strong
					>? This action cannot be undone.
				</p>
				<div class="flex gap-3">
					<button
						onclick={() => (deletingId = null)}
						disabled={deleteLoading}
						class="flex-1 px-4 py-2.5 bg-muted text-foreground rounded-xl hover:bg-accent transition-colors"
					>
						Cancel
					</button>
					<button
						onclick={confirmDelete}
						disabled={deleteLoading}
						class="flex-1 px-4 py-2.5 bg-red-500 text-white rounded-xl hover:bg-red-600 transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
					>
						{#if deleteLoading}
							<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
								<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"
								></circle>
								<path
									class="opacity-75"
									fill="currentColor"
									d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
								></path>
							</svg>
						{/if}
						Delete
					</button>
				</div>
			</div>
		</div>
	</div>
{/if}
