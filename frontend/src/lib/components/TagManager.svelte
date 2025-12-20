<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { Tag } from '$lib/api/client';
	import { getTags, createTag, updateTag, deleteTag } from '$lib/api/client';

	const dispatch = createEventDispatcher<{
		close: void;
		tagsUpdated: Tag[];
	}>();

	// Predefined colors for tags
	const TAG_COLORS = [
		{ name: 'Red', value: '#ef4444' },
		{ name: 'Orange', value: '#f97316' },
		{ name: 'Yellow', value: '#eab308' },
		{ name: 'Green', value: '#22c55e' },
		{ name: 'Blue', value: '#3b82f6' },
		{ name: 'Purple', value: '#a855f7' },
		{ name: 'Pink', value: '#ec4899' },
		{ name: 'Gray', value: '#6b7280' }
	];

	let tags: Tag[] = [];
	let loading = true;
	let error = '';

	// Form state
	let newTagName = '';
	let newTagColor = TAG_COLORS[4].value; // Default to blue
	let editingTag: Tag | null = null;
	let editName = '';
	let editColor = '';
	let confirmDelete: string | null = null;

	// Load tags on mount
	async function loadTags() {
		loading = true;
		error = '';
		try {
			tags = await getTags();
		} catch (err: any) {
			error = err.detail || 'Failed to load tags';
		} finally {
			loading = false;
		}
	}

	// Create a new tag
	async function handleCreateTag() {
		if (!newTagName.trim()) return;

		try {
			const tag = await createTag(newTagName.trim(), newTagColor);
			tags = [...tags, tag].sort((a, b) => a.name.localeCompare(b.name));
			newTagName = '';
			newTagColor = TAG_COLORS[4].value;
			dispatch('tagsUpdated', tags);
		} catch (err: any) {
			error = err.detail || 'Failed to create tag';
		}
	}

	// Start editing a tag
	function startEdit(tag: Tag) {
		editingTag = tag;
		editName = tag.name;
		editColor = tag.color;
		confirmDelete = null;
	}

	// Save tag edit
	async function saveEdit() {
		if (!editingTag || !editName.trim()) return;

		try {
			const updated = await updateTag(editingTag.id, {
				name: editName.trim(),
				color: editColor
			});
			tags = tags.map(t => t.id === updated.id ? updated : t).sort((a, b) => a.name.localeCompare(b.name));
			editingTag = null;
			dispatch('tagsUpdated', tags);
		} catch (err: any) {
			error = err.detail || 'Failed to update tag';
		}
	}

	// Cancel editing
	function cancelEdit() {
		editingTag = null;
		editName = '';
		editColor = '';
	}

	// Delete a tag
	async function handleDelete(tagId: string) {
		try {
			await deleteTag(tagId);
			tags = tags.filter(t => t.id !== tagId);
			confirmDelete = null;
			dispatch('tagsUpdated', tags);
		} catch (err: any) {
			error = err.detail || 'Failed to delete tag';
		}
	}

	// Initialize
	loadTags();
</script>

<div class="fixed inset-0 max-sm:bottom-[calc(4.5rem+env(safe-area-inset-bottom,0px))] z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
	<div class="bg-background border border-border rounded-2xl shadow-2xl w-full max-w-md max-h-[80vh] max-sm:max-h-full flex flex-col">
		<!-- Header -->
		<div class="flex items-center justify-between px-5 py-4 border-b border-border">
			<h2 class="text-lg font-semibold text-foreground">Manage Tags</h2>
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
				<div class="p-3 rounded-lg bg-destructive/10 text-destructive text-sm">
					{error}
				</div>
			{/if}

			<!-- Create new tag -->
			<div class="space-y-3">
				<label class="text-sm font-medium text-muted-foreground">Create New Tag</label>
				<div class="flex items-center gap-2">
					<input
						type="text"
						bind:value={newTagName}
						placeholder="Tag name..."
						class="flex-1 px-3 py-2 rounded-lg bg-muted/50 border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
						on:keypress={(e) => e.key === 'Enter' && handleCreateTag()}
					/>
					<div class="flex items-center gap-1">
						{#each TAG_COLORS as color}
							<button
								on:click={() => newTagColor = color.value}
								class="w-6 h-6 rounded-full transition-transform {newTagColor === color.value ? 'ring-2 ring-offset-2 ring-offset-background ring-primary scale-110' : 'hover:scale-110'}"
								style="background-color: {color.value}"
								title={color.name}
							></button>
						{/each}
					</div>
					<button
						on:click={handleCreateTag}
						disabled={!newTagName.trim()}
						class="px-3 py-2 rounded-lg bg-primary text-primary-foreground font-medium text-sm disabled:opacity-50 disabled:cursor-not-allowed hover:bg-primary/90 transition-colors"
					>
						Add
					</button>
				</div>
			</div>

			<!-- Tag list -->
			<div class="space-y-2">
				<label class="text-sm font-medium text-muted-foreground">Your Tags</label>
				{#if loading}
					<div class="flex items-center justify-center py-8">
						<div class="animate-spin rounded-full h-6 w-6 border-2 border-primary border-t-transparent"></div>
					</div>
				{:else if tags.length === 0}
					<div class="text-center py-8 text-muted-foreground text-sm">
						No tags yet. Create one above!
					</div>
				{:else}
					<div class="space-y-2">
						{#each tags as tag (tag.id)}
							<div class="flex items-center gap-3 p-3 rounded-lg bg-muted/50 border border-border">
								{#if editingTag?.id === tag.id}
									<!-- Edit mode -->
									<input
										type="text"
										bind:value={editName}
										class="flex-1 px-2 py-1 rounded bg-muted/50 border border-border text-foreground text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
										on:keypress={(e) => e.key === 'Enter' && saveEdit()}
									/>
									<div class="flex items-center gap-1">
										{#each TAG_COLORS as color}
											<button
												on:click={() => editColor = color.value}
												class="w-5 h-5 rounded-full transition-transform {editColor === color.value ? 'ring-2 ring-offset-1 ring-offset-background ring-primary scale-110' : 'hover:scale-110'}"
												style="background-color: {color.value}"
												title={color.name}
											></button>
										{/each}
									</div>
									<button
										on:click={saveEdit}
										class="p-1.5 rounded-lg text-success hover:bg-success/10 transition-colors"
										title="Save"
									>
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
										</svg>
									</button>
									<button
										on:click={cancelEdit}
										class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-hover-overlay transition-colors"
										title="Cancel"
									>
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
										</svg>
									</button>
								{:else}
									<!-- View mode -->
									<span
										class="w-4 h-4 rounded-full flex-shrink-0"
										style="background-color: {tag.color}"
									></span>
									<span class="flex-1 text-sm text-foreground">{tag.name}</span>
									<button
										on:click={() => startEdit(tag)}
										class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-hover-overlay transition-colors"
										title="Edit"
									>
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
										</svg>
									</button>
									{#if confirmDelete === tag.id}
										<span class="text-xs text-destructive">Delete?</span>
										<button
											on:click={() => handleDelete(tag.id)}
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
											on:click={() => confirmDelete = tag.id}
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
						{/each}
					</div>
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
