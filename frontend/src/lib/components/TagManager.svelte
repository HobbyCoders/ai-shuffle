<script lang="ts">
	import '$lib/components/ui/surfaces.css';
	import { watchResources } from '$lib/services/watch-resources';
	import type { Tag } from '$lib/api/client';
	import { getTags, createTag, updateTag, deleteTag } from '$lib/api/client';
	import { focusTrap } from '$lib/components/ui/focus-trap';

	let { onclose, ontagsupdated }: { onclose?: () => void; ontagsupdated?: (tags: Tag[]) => void } = $props();

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

	let tags = $state<Tag[]>([]);
	let loading = $state(true);
	let error = $state('');

	// Form state
	let newTagName = $state('');
	let newTagColor = $state(TAG_COLORS[4].value); // Default to blue
	let editingTag = $state<Tag | null>(null);
	let editName = $state('');
	let editColor = $state('');
	let confirmDelete = $state<string | null>(null);

	// Load tags on mount
	async function loadTags(quiet = false) {
		if (!quiet) loading = true;
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
			ontagsupdated?.(tags);
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
			savingEdit = true;
			const updated = await updateTag(editingTag.id, {
				name: editName.trim(),
				color: editColor
			});
			tags = tags.map(t => t.id === updated.id ? updated : t).sort((a, b) => a.name.localeCompare(b.name));
			editingTag = null;
			ontagsupdated?.(tags);
		} catch (err: any) {
			error = err.detail || 'Failed to update tag';
		} finally { savingEdit = false; }
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
			ontagsupdated?.(tags);
		} catch (err: any) {
			error = err.detail || 'Failed to delete tag';
		}
	}

	// Initialize
	loadTags();
	let remoteChange = $state('');
	let savingEdit = false;
	watchResources(['tags'], async () => {
		const selected = editingTag;
		const before = JSON.stringify([editName, editColor]);
		await loadTags(true);
		if (!selected || editingTag !== selected) return;
		const fresh = tags.find((tag) => tag.id === selected.id);
		if (savingEdit || before !== JSON.stringify([editName, editColor]) || editName !== selected.name || editColor !== selected.color) {
			if (!fresh) tags = [...tags, selected];
			if (!fresh || fresh.name !== selected.name || fresh.color !== selected.color) remoteChange = fresh
				? 'This tag changed elsewhere. Your unsaved name and color are kept here.'
				: 'This tag was deleted elsewhere. Copy your unsaved name before leaving.';
			return;
		}
		if (fresh) startEdit(fresh);
		else { cancelEdit(); remoteChange = 'This tag was deleted elsewhere.'; }
	});

</script>

<!-- Escape is handled by the host page's global close-modal shortcut. -->

<div
	class="ui-overlay ui-surface"
	role="dialog"
	aria-modal="true"
	aria-labelledby="tag-manager-title"
	tabindex="-1"
	use:focusTrap
>
	<button class="ui-scrim" onclick={() => onclose?.()} tabindex="-1" aria-label="Close dialog"></button>
	<div class="ui-dialog">
		<!-- Header -->
		<div class="ui-dialog-header">
			<h2 id="tag-manager-title" class="ui-dialog-title">Manage tags</h2>
			<button
				onclick={() => onclose?.()}
				aria-label="Close"
				class="ui-button ghost icon"
			>
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
				</svg>
			</button>
		</div>

		<!-- Content -->
		{#if remoteChange}<p role="status" style="flex: none; height: auto;" class="px-4 py-2 text-sm text-muted-foreground">{remoteChange}</p>{/if}
		<div class="ui-dialog-body space-y-5">
			{#if error}
				<div class="p-3 rounded-lg bg-destructive/10 text-destructive text-sm" role="alert">
					{error}
				</div>
			{/if}

			<!-- Create new tag -->
			<div class="space-y-3">
				<label for="new-tag-name" class="text-sm font-medium text-muted-foreground">Create new tag</label>
				<div class="tag-create">
					<input
						id="new-tag-name"
						type="text"
						bind:value={newTagName}
						placeholder="Tag name..."
						class="ui-input"
						onkeypress={(e) => e.key === 'Enter' && handleCreateTag()}
					/>
					<div class="flex items-center gap-1">
						{#each TAG_COLORS as color}
							<button
								onclick={() => newTagColor = color.value}
								aria-pressed={newTagColor === color.value}
								class="w-6 h-6 rounded-full transition-transform {newTagColor === color.value ? 'ring-2 ring-offset-2 ring-offset-background ring-primary scale-110' : 'hover:scale-110'}"
								style="background-color: {color.value}"
								title={color.name}
							></button>
						{/each}
					</div>
					<button
						onclick={handleCreateTag}
						disabled={!newTagName.trim()}
						class="ui-button primary"
					>
						Add
					</button>
				</div>
			</div>

			<!-- Tag list -->
			<div class="space-y-2">
				<span class="text-sm font-medium text-muted-foreground">Your tags</span>
				{#if loading}
					<div class="flex items-center justify-center py-8">
						<div class="animate-spin rounded-full h-6 w-6 border-2 border-primary border-t-transparent"></div>
					</div>
				{:else if tags.length === 0}
					<div class="text-center py-8 text-muted-foreground text-sm">
						No tags yet. Create one above.
					</div>
				{:else}
					<div class="tag-list">
						{#each tags as tag (tag.id)}
							<div class="tag-row">
								{#if editingTag?.id === tag.id}
									<!-- Edit mode -->
									<input
										type="text"
										bind:value={editName}
										aria-label="Tag name"
										class="ui-input flex-1"
										onkeypress={(e) => e.key === 'Enter' && saveEdit()}
									/>
									<div class="flex items-center gap-1">
										{#each TAG_COLORS as color}
											<button
												onclick={() => editColor = color.value}
												aria-pressed={editColor === color.value}
												class="w-5 h-5 rounded-full transition-transform {editColor === color.value ? 'ring-2 ring-offset-1 ring-offset-background ring-primary scale-110' : 'hover:scale-110'}"
												style="background-color: {color.value}"
												title={color.name}
											></button>
										{/each}
									</div>
									<button
										onclick={saveEdit}
										class="ui-button ghost icon"
										title="Save"
									>
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
										</svg>
									</button>
									<button
										onclick={cancelEdit}
										class="ui-button ghost icon"
										title="Cancel"
									>
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
										</svg>
									</button>
								{:else}
									<!-- View mode -->
									<span
										class="w-4 h-4 rounded-full shrink-0"
										style="background-color: {tag.color}"
									></span>
									<span class="flex-1 text-sm text-foreground">{tag.name}</span>
									<button
										onclick={() => startEdit(tag)}
										class="ui-button ghost icon"
										title="Edit"
									>
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
										</svg>
									</button>
									{#if confirmDelete === tag.id}
										<span class="text-xs text-destructive">Delete?</span>
										<button
											onclick={() => handleDelete(tag.id)}
											class="ui-button ghost icon"
											title="Confirm delete"
										>
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
											</svg>
										</button>
										<button
											onclick={() => confirmDelete = null}
											class="ui-button ghost icon"
											title="Cancel"
										>
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
											</svg>
										</button>
									{:else}
										<button
											onclick={() => confirmDelete = tag.id}
											class="ui-button ghost icon"
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
		<div class="ui-dialog-footer">
			<button
				onclick={() => onclose?.()}
				class="ui-button"
			>
				Close
			</button>
		</div>
	</div>
</div>
<style>
 .tag-create { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
 .tag-create > input { flex: 1 0 100%; }
 .tag-create > button { margin-left: auto; }
 .tag-list { border-top: 1px solid var(--border); }
 .tag-row { display: flex; align-items: center; gap: 12px; min-height: 52px; padding: 8px 0; border-bottom: 1px solid var(--border); }
</style>
