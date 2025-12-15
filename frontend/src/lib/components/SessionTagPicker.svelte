<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { Tag, SessionTag } from '$lib/api/client';
	import { getTags, setSessionTags } from '$lib/api/client';

	export let sessionId: string;
	export let currentTags: SessionTag[] = [];
	export let position: { x: number; y: number } = { x: 0, y: 0 };

	const dispatch = createEventDispatcher<{
		close: void;
		tagsUpdated: SessionTag[];
		openTagManager: void;
	}>();

	let allTags: Tag[] = [];
	let loading = true;
	let error = '';
	let selectedTagIds: Set<string> = new Set(currentTags.map(t => t.id));

	// Load all available tags
	async function loadTags() {
		loading = true;
		error = '';
		try {
			allTags = await getTags();
		} catch (err: any) {
			error = err.detail || 'Failed to load tags';
		} finally {
			loading = false;
		}
	}

	// Toggle tag selection
	async function toggleTag(tagId: string) {
		if (selectedTagIds.has(tagId)) {
			selectedTagIds.delete(tagId);
		} else {
			selectedTagIds.add(tagId);
		}
		selectedTagIds = selectedTagIds; // Trigger reactivity

		// Save immediately
		try {
			const updatedTags = await setSessionTags(sessionId, Array.from(selectedTagIds));
			dispatch('tagsUpdated', updatedTags.map(t => ({ id: t.id, name: t.name, color: t.color })));
		} catch (err: any) {
			error = err.detail || 'Failed to update tags';
			// Revert on error
			if (selectedTagIds.has(tagId)) {
				selectedTagIds.delete(tagId);
			} else {
				selectedTagIds.add(tagId);
			}
			selectedTagIds = selectedTagIds;
		}
	}

	// Initialize
	loadTags();

	// Calculate dropdown position
	$: style = `left: ${position.x}px; top: ${position.y}px;`;
</script>

<svelte:window on:click={() => dispatch('close')} />

<div
	class="fixed z-[60] w-56 bg-background border border-border rounded-xl shadow-2xl overflow-hidden"
	style={style}
	on:click|stopPropagation
	role="dialog"
	aria-label="Tag picker"
>
	<!-- Header -->
	<div class="px-3 py-2 border-b border-border bg-white/5">
		<div class="flex items-center justify-between">
			<span class="text-xs font-medium text-muted-foreground">Tags</span>
			<button
				on:click={() => dispatch('openTagManager')}
				class="text-xs text-primary hover:text-primary/80 transition-colors"
			>
				Manage
			</button>
		</div>
	</div>

	<!-- Tag list -->
	<div class="max-h-64 overflow-y-auto p-1">
		{#if loading}
			<div class="flex items-center justify-center py-4">
				<div class="animate-spin rounded-full h-4 w-4 border-2 border-primary border-t-transparent"></div>
			</div>
		{:else if error}
			<div class="px-3 py-2 text-xs text-destructive">{error}</div>
		{:else if allTags.length === 0}
			<div class="px-3 py-4 text-center">
				<p class="text-xs text-muted-foreground mb-2">No tags yet</p>
				<button
					on:click={() => dispatch('openTagManager')}
					class="text-xs text-primary hover:text-primary/80 transition-colors"
				>
					Create tags
				</button>
			</div>
		{:else}
			{#each allTags as tag (tag.id)}
				<button
					on:click={() => toggleTag(tag.id)}
					class="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-left hover:bg-white/5 transition-colors"
				>
					<span
						class="w-3 h-3 rounded-full flex-shrink-0"
						style="background-color: {tag.color}"
					></span>
					<span class="flex-1 text-sm text-foreground truncate">{tag.name}</span>
					{#if selectedTagIds.has(tag.id)}
						<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
						</svg>
					{/if}
				</button>
			{/each}
		{/if}
	</div>
</div>
