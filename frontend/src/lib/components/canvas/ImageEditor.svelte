<script lang="ts">
	import { onMount } from 'svelte';
	import { canvas, editingItem, isLoading } from '$lib/stores/canvas';
	import { api } from '$lib/api/client';
	import { getProviders, type ImageProvider } from '$lib/api/canvas';
	import ProviderSelector from './ProviderSelector.svelte';

	let editPrompt = '';
	let provider = '';
	let model: string | null = null;

	// Data from API
	let editProviders: ImageProvider[] = [];
	let loadingProviders = true;

	$: currentItem = $editingItem;

	// Initialize provider from the original image if available
	$: {
		if (currentItem && editProviders.length > 0) {
			const originalProvider = editProviders.find((p) => p.id === currentItem.provider);
			if (originalProvider) {
				provider = currentItem.provider;
			} else if (!provider) {
				provider = editProviders[0].id;
			}
		}
	}

	onMount(async () => {
		try {
			const providersData = await getProviders();
			// Only include providers that support editing
			editProviders = providersData.image_providers.filter((p) => p.supports_edit);

			// Set defaults
			if (editProviders.length > 0) {
				// Try to use original provider if it supports edit
				if (currentItem && editProviders.some((p) => p.id === currentItem.provider)) {
					provider = currentItem.provider;
				} else {
					provider = editProviders[0].id;
				}
				const defaultModel = editProviders.find((p) => p.id === provider)?.models.find((m) => m.default) || editProviders.find((p) => p.id === provider)?.models[0];
				model = defaultModel?.id || null;
			}
		} catch (error) {
			console.error('Failed to fetch providers:', error);
		} finally {
			loadingProviders = false;
		}
	});

	function handleProviderChange(newProvider: string) {
		provider = newProvider;
	}

	function handleModelChange(newModel: string | null) {
		model = newModel;
	}

	async function handleEdit() {
		if (!editPrompt.trim() || !currentItem) return;

		canvas.startGeneration();
		canvas.updateProgress('Editing image...');

		try {
			const response = await api.post('/canvas/edit/image', {
				item_id: currentItem.id,
				prompt: editPrompt,
				provider,
				model
			});

			canvas.completeGeneration(response as any);
			canvas.setView('gallery');
			canvas.setEditingItem(null);
			editPrompt = '';
		} catch (error: any) {
			canvas.failGeneration(error.detail || 'Failed to edit image');
		}
	}

	function handleCancel() {
		canvas.setView('gallery');
		canvas.setEditingItem(null);
		editPrompt = '';
	}
</script>

{#if currentItem}
	<div class="max-w-4xl mx-auto">
		<div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
			<!-- Original Image Preview -->
			<div class="space-y-3">
				<div class="flex items-center justify-between">
					<span class="text-sm font-medium text-foreground">Original Image</span>
					<span class="text-xs text-muted-foreground px-2 py-1 bg-muted rounded-md">
						{currentItem.settings.aspectRatio || '16:9'}
					</span>
				</div>
				<div class="relative aspect-video bg-muted rounded-xl overflow-hidden border border-border">
					<img src={currentItem.url} alt={currentItem.prompt} class="w-full h-full object-contain" />
				</div>

				<!-- Original Prompt -->
				<div class="bg-muted/50 rounded-lg p-3 border border-border">
					<p class="text-xs text-muted-foreground mb-1">Original prompt:</p>
					<p class="text-sm text-foreground">{currentItem.prompt}</p>
				</div>

				<!-- Original Settings -->
				<div class="flex flex-wrap gap-2 text-xs">
					<span class="px-2 py-1 bg-muted rounded-md text-muted-foreground">
						Provider: <span class="text-foreground">{currentItem.provider}</span>
					</span>
					{#if currentItem.settings.resolution}
						<span class="px-2 py-1 bg-muted rounded-md text-muted-foreground">
							Resolution: <span class="text-foreground">{currentItem.settings.resolution}</span>
						</span>
					{/if}
				</div>
			</div>

			<!-- Edit Form -->
			<div class="space-y-6">
				<!-- Edit Prompt -->
				<div>
					<label for="edit-prompt" class="block text-sm font-medium text-foreground mb-2">Edit Instructions</label>
					<textarea
						id="edit-prompt"
						bind:value={editPrompt}
						placeholder="Describe the changes you want to make, e.g., 'Change the sky to a vibrant sunset' or 'Add a small cabin by the lake'"
						rows="5"
						class="w-full bg-muted border border-border rounded-xl px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
					></textarea>
					<p class="text-xs text-muted-foreground mt-2">Be specific about what you want to change. The AI will modify the original image based on your instructions.</p>
				</div>

				<!-- Provider Selector -->
				<div>
					{#if loadingProviders}
						<div class="flex items-center gap-2 py-4">
							<div class="w-4 h-4 border-2 border-muted-foreground border-t-transparent rounded-full animate-spin"></div>
							<span class="text-sm text-muted-foreground">Loading providers...</span>
						</div>
					{:else if editProviders.length === 0}
						<p class="text-sm text-muted-foreground">No providers available for image editing.</p>
					{:else}
						<ProviderSelector type="image" value={provider} modelValue={model} onChange={handleProviderChange} onModelChange={handleModelChange} providers={editProviders} loading={loadingProviders} />
						<p class="text-xs text-muted-foreground mt-2">Note: Only providers that support image editing are shown.</p>
					{/if}
				</div>

				<!-- Example Edits -->
				<div class="bg-muted/30 rounded-lg p-4 border border-border">
					<p class="text-xs font-medium text-foreground mb-2">Example edit prompts:</p>
					<ul class="text-xs text-muted-foreground space-y-1">
						<li>"Change the background to a beach scene"</li>
						<li>"Make the lighting more dramatic and moody"</li>
						<li>"Add snow falling from the sky"</li>
						<li>"Remove the person on the left side"</li>
						<li>"Change the color scheme to warm autumn tones"</li>
					</ul>
				</div>

				<!-- Action Buttons -->
				<div class="flex flex-col-reverse sm:flex-row items-stretch sm:items-center justify-end gap-3 pt-4 border-t border-border">
					<button type="button" onclick={handleCancel} disabled={$isLoading} class="px-6 py-2.5 text-sm font-medium bg-muted text-foreground border border-border rounded-xl hover:bg-accent transition-colors disabled:opacity-50">
						Cancel
					</button>
					<button
						type="button"
						onclick={handleEdit}
						disabled={!editPrompt.trim() || $isLoading || loadingProviders || editProviders.length === 0}
						class="px-6 py-2.5 text-sm font-medium bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2 min-w-[140px]"
					>
						{#if $isLoading}
							<div class="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin"></div>
							Editing...
						{:else}
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
							</svg>
							Apply Edit
						{/if}
					</button>
				</div>
			</div>
		</div>
	</div>
{:else}
	<div class="flex flex-col items-center justify-center py-16 text-center">
		<div class="w-16 h-16 rounded-2xl bg-muted/50 flex items-center justify-center mb-4">
			<svg class="w-8 h-8 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
			</svg>
		</div>
		<h3 class="text-lg font-medium text-foreground mb-2">No image selected</h3>
		<p class="text-sm text-muted-foreground mb-4">Select an image from the gallery to edit it.</p>
		<button onclick={() => canvas.setView('gallery')} class="px-4 py-2 text-sm font-medium bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors"> Back to Gallery </button>
	</div>
{/if}
