<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { Template, Profile } from '$lib/api/client';
	import { getTemplates, createTemplate, updateTemplate, deleteTemplate } from '$lib/api/client';
	import { api } from '$lib/api/client';

	const dispatch = createEventDispatcher<{
		close: void;
		templatesUpdated: Template[];
	}>();

	// Common template icons
	const TEMPLATE_ICONS = [
		{ emoji: '', name: 'None' },
		{ emoji: 'magnifying_glass_tilted_right', name: 'Review' },
		{ emoji: 'books', name: 'Explain' },
		{ emoji: 'bug', name: 'Debug' },
		{ emoji: 'test_tube', name: 'Test' },
		{ emoji: 'memo', name: 'Document' },
		{ emoji: 'hammer_and_wrench', name: 'Build' },
		{ emoji: 'rocket', name: 'Deploy' },
		{ emoji: 'brain', name: 'AI' },
		{ emoji: 'light_bulb', name: 'Idea' },
		{ emoji: 'speech_balloon', name: 'Chat' },
		{ emoji: 'file_folder', name: 'Files' }
	];

	// Categories for templates
	const TEMPLATE_CATEGORIES = [
		{ id: '', name: 'Uncategorized' },
		{ id: 'coding', name: 'Coding' },
		{ id: 'writing', name: 'Writing' },
		{ id: 'analysis', name: 'Analysis' },
		{ id: 'debugging', name: 'Debugging' },
		{ id: 'documentation', name: 'Documentation' }
	];

	let templates: Template[] = [];
	let profiles: Profile[] = [];
	let loading = true;
	let error = '';

	// Form state
	let showCreateForm = false;
	let editingTemplate: Template | null = null;

	// Form fields
	let formName = '';
	let formDescription = '';
	let formPrompt = '';
	let formCategory = '';
	let formIcon = '';
	let formProfileId = '';

	let confirmDelete: string | null = null;

	// Load templates and profiles on mount
	async function loadData() {
		loading = true;
		error = '';
		try {
			const [templatesData, profilesData] = await Promise.all([
				getTemplates(),
				api.get<Profile[]>('/profiles')
			]);
			templates = templatesData;
			profiles = profilesData;
		} catch (err: any) {
			error = err.detail || 'Failed to load data';
		} finally {
			loading = false;
		}
	}

	// Reset form
	function resetForm() {
		formName = '';
		formDescription = '';
		formPrompt = '';
		formCategory = '';
		formIcon = '';
		formProfileId = '';
		showCreateForm = false;
		editingTemplate = null;
	}

	// Start creating a new template
	function startCreate() {
		resetForm();
		showCreateForm = true;
	}

	// Start editing a template
	function startEdit(template: Template) {
		editingTemplate = template;
		formName = template.name;
		formDescription = template.description || '';
		formPrompt = template.prompt;
		formCategory = template.category || '';
		formIcon = template.icon || '';
		formProfileId = template.profile_id || '';
		showCreateForm = false;
		confirmDelete = null;
	}

	// Save template (create or update)
	async function saveTemplate() {
		if (!formName.trim() || !formPrompt.trim()) return;

		try {
			if (editingTemplate) {
				// Update existing template
				const updated = await updateTemplate(editingTemplate.id, {
					name: formName.trim(),
					description: formDescription.trim() || null,
					prompt: formPrompt.trim(),
					category: formCategory || null,
					icon: formIcon || null,
					profile_id: formProfileId || null
				});
				templates = templates.map(t => t.id === updated.id ? updated : t);
			} else {
				// Create new template
				const created = await createTemplate({
					name: formName.trim(),
					description: formDescription.trim() || null,
					prompt: formPrompt.trim(),
					category: formCategory || null,
					icon: formIcon || null,
					profile_id: formProfileId || null
				});
				templates = [...templates, created];
			}
			resetForm();
			dispatch('templatesUpdated', templates);
		} catch (err: any) {
			error = err.detail || 'Failed to save template';
		}
	}

	// Delete a template
	async function handleDelete(templateId: string) {
		try {
			await deleteTemplate(templateId);
			templates = templates.filter(t => t.id !== templateId);
			confirmDelete = null;
			if (editingTemplate?.id === templateId) {
				resetForm();
			}
			dispatch('templatesUpdated', templates);
		} catch (err: any) {
			error = err.detail || 'Failed to delete template';
		}
	}

	// Cancel editing/creating
	function cancelForm() {
		resetForm();
	}

	// Get icon display
	function getIconDisplay(iconName: string | null): string {
		if (!iconName) return '';
		const iconMap: Record<string, string> = {
			'magnifying_glass_tilted_right': '\uD83D\uDD0D',
			'books': '\uD83D\uDCDA',
			'bug': '\uD83D\uDC1B',
			'test_tube': '\uD83E\uDDEA',
			'memo': '\uD83D\uDCDD',
			'hammer_and_wrench': '\uD83D\uDEE0',
			'rocket': '\uD83D\uDE80',
			'brain': '\uD83E\uDDE0',
			'light_bulb': '\uD83D\uDCA1',
			'speech_balloon': '\uD83D\uDCAC',
			'file_folder': '\uD83D\uDCC1'
		};
		return iconMap[iconName] || '';
	}

	// Group templates by category
	function getGroupedTemplates(): Map<string, Template[]> {
		const grouped = new Map<string, Template[]>();
		for (const template of templates) {
			const cat = template.category || '';
			if (!grouped.has(cat)) {
				grouped.set(cat, []);
			}
			grouped.get(cat)!.push(template);
		}
		return grouped;
	}

	// Get category display name
	function getCategoryName(catId: string): string {
		const cat = TEMPLATE_CATEGORIES.find(c => c.id === catId);
		return cat?.name || catId || 'Uncategorized';
	}

	// Initialize
	loadData();
</script>

<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
	<div class="bg-background border border-border rounded-2xl shadow-2xl w-full max-w-2xl max-h-[85vh] flex flex-col">
		<!-- Header -->
		<div class="flex items-center justify-between px-5 py-4 border-b border-border">
			<h2 class="text-lg font-semibold text-foreground">Manage Templates</h2>
			<button
				on:click={() => dispatch('close')}
				class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-hover-overlay transition-colors"
			>
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
				</svg>
			</button>
		</div>

		<!-- Content -->
		<div class="flex-1 overflow-y-auto p-5 space-y-5">
			{#if error}
				<div class="p-3 rounded-lg bg-destructive/10 text-destructive text-sm flex items-center justify-between">
					<span>{error}</span>
					<button on:click={() => error = ''} class="text-destructive hover:text-destructive/80">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>
			{/if}

			{#if showCreateForm || editingTemplate}
				<!-- Create/Edit form -->
				<div class="space-y-4 p-4 rounded-lg bg-muted/50 border border-border">
					<h3 class="font-medium text-foreground">
						{editingTemplate ? 'Edit Template' : 'Create New Template'}
					</h3>

					<div class="grid grid-cols-2 gap-4">
						<div class="space-y-2">
							<label class="text-sm font-medium text-muted-foreground">Name</label>
							<input
								type="text"
								bind:value={formName}
								placeholder="e.g., Code Review"
								class="w-full px-3 py-2 rounded-lg bg-muted/50 border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
							/>
						</div>
						<div class="space-y-2">
							<label class="text-sm font-medium text-muted-foreground">Category</label>
							<select
								bind:value={formCategory}
								class="w-full px-3 py-2 rounded-lg bg-muted/50 border border-border text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
							>
								{#each TEMPLATE_CATEGORIES as cat}
									<option value={cat.id}>{cat.name}</option>
								{/each}
							</select>
						</div>
					</div>

					<div class="space-y-2">
						<label class="text-sm font-medium text-muted-foreground">Description (optional)</label>
						<input
							type="text"
							bind:value={formDescription}
							placeholder="Brief description of when to use this template"
							class="w-full px-3 py-2 rounded-lg bg-muted/50 border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
						/>
					</div>

					<div class="space-y-2">
						<label class="text-sm font-medium text-muted-foreground">Starter Prompt</label>
						<textarea
							bind:value={formPrompt}
							placeholder="Enter the prompt that will be used to start the conversation..."
							rows="4"
							class="w-full px-3 py-2 rounded-lg bg-muted/50 border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
						></textarea>
					</div>

					<div class="grid grid-cols-2 gap-4">
						<div class="space-y-2">
							<label class="text-sm font-medium text-muted-foreground">Icon (optional)</label>
							<div class="flex flex-wrap gap-2">
								{#each TEMPLATE_ICONS as icon}
									<button
										type="button"
										on:click={() => formIcon = icon.emoji}
										class="w-8 h-8 rounded-lg flex items-center justify-center text-lg transition-colors {formIcon === icon.emoji ? 'bg-primary text-primary-foreground' : 'bg-muted/50 hover:bg-hover-overlay'}"
										title={icon.name}
									>
										{icon.emoji ? getIconDisplay(icon.emoji) : '-'}
									</button>
								{/each}
							</div>
						</div>
						<div class="space-y-2">
							<label class="text-sm font-medium text-muted-foreground">Profile (optional)</label>
							<select
								bind:value={formProfileId}
								class="w-full px-3 py-2 rounded-lg bg-muted/50 border border-border text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
							>
								<option value="">All Profiles</option>
								{#each profiles as profile}
									<option value={profile.id}>{profile.name}</option>
								{/each}
							</select>
							<p class="text-xs text-muted-foreground">Restrict this template to a specific profile</p>
						</div>
					</div>

					<div class="flex justify-end gap-2 pt-2">
						<button
							on:click={cancelForm}
							class="px-4 py-2 rounded-lg bg-muted/50 text-foreground font-medium text-sm hover:bg-hover-overlay transition-colors"
						>
							Cancel
						</button>
						<button
							on:click={saveTemplate}
							disabled={!formName.trim() || !formPrompt.trim()}
							class="px-4 py-2 rounded-lg bg-primary text-primary-foreground font-medium text-sm disabled:opacity-50 disabled:cursor-not-allowed hover:bg-primary/90 transition-colors"
						>
							{editingTemplate ? 'Update' : 'Create'}
						</button>
					</div>
				</div>
			{:else}
				<!-- Create button -->
				<button
					on:click={startCreate}
					class="w-full px-4 py-3 rounded-lg border border-dashed border-border text-muted-foreground hover:text-foreground hover:border-primary/50 hover:bg-accent/50 transition-colors flex items-center justify-center gap-2"
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
					</svg>
					Create New Template
				</button>
			{/if}

			<!-- Template list -->
			<div class="space-y-4">
				{#if loading}
					<div class="flex items-center justify-center py-8">
						<div class="animate-spin rounded-full h-6 w-6 border-2 border-primary border-t-transparent"></div>
					</div>
				{:else if templates.length === 0 && !showCreateForm}
					<div class="text-center py-8 text-muted-foreground text-sm">
						No templates yet. Create one to get started!
					</div>
				{:else}
					{@const grouped = getGroupedTemplates()}
					{#each [...grouped.entries()] as [category, categoryTemplates]}
						<div class="space-y-2">
							<h4 class="text-sm font-medium text-muted-foreground">{getCategoryName(category)}</h4>
							{#each categoryTemplates as template (template.id)}
								<div class="flex items-start gap-3 p-3 rounded-lg bg-muted/50 border border-border hover:border-border/80 transition-colors {editingTemplate?.id === template.id ? 'ring-2 ring-primary/50' : ''}">
									<div class="w-8 h-8 rounded-lg bg-accent flex items-center justify-center text-lg flex-shrink-0">
										{getIconDisplay(template.icon) || '\uD83D\uDCDD'}
									</div>
									<div class="flex-1 min-w-0">
										<div class="flex items-center gap-2">
											<span class="font-medium text-foreground">{template.name}</span>
											{#if template.is_builtin}
												<span class="px-1.5 py-0.5 rounded text-xs bg-primary/10 text-primary">Built-in</span>
											{/if}
										</div>
										{#if template.description}
											<p class="text-sm text-muted-foreground mt-0.5 truncate">{template.description}</p>
										{/if}
										<p class="text-xs text-muted-foreground/70 mt-1 line-clamp-2">{template.prompt}</p>
									</div>
									<div class="flex items-center gap-1 flex-shrink-0">
										{#if !template.is_builtin}
											<button
												on:click={() => startEdit(template)}
												class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-hover-overlay transition-colors"
												title="Edit"
											>
												<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
												</svg>
											</button>
											{#if confirmDelete === template.id}
												<span class="text-xs text-destructive">Delete?</span>
												<button
													on:click={() => handleDelete(template.id)}
													class="p-1.5 rounded-lg text-destructive hover:bg-destructive/10 transition-colors"
													title="Confirm delete"
												>
													<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
													</svg>
												</button>
												<button
													on:click={() => confirmDelete = null}
													class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-hover-overlay transition-colors"
													title="Cancel"
												>
													<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
													</svg>
												</button>
											{:else}
												<button
													on:click={() => confirmDelete = template.id}
													class="p-1.5 rounded-lg text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors"
													title="Delete"
												>
													<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
													</svg>
												</button>
											{/if}
										{/if}
									</div>
								</div>
							{/each}
						</div>
					{/each}
				{/if}
			</div>
		</div>

		<!-- Footer -->
		<div class="flex justify-end px-5 py-4 border-t border-border">
			<button
				on:click={() => dispatch('close')}
				class="px-4 py-2 rounded-lg bg-muted/50 text-foreground font-medium text-sm hover:bg-hover-overlay transition-colors"
			>
				Close
			</button>
		</div>
	</div>
</div>
