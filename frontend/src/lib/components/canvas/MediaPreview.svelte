<script lang="ts">
	import { canvas, selectedItem, isLoading } from '$lib/stores/canvas';

	let copied = false;

	function handleClose() {
		canvas.selectItem(null);
	}

	async function handleDownload() {
		if (!$selectedItem) return;
		try {
			// Fetch with credentials to include auth cookie
			const response = await fetch($selectedItem.url, { credentials: 'include' });
			if (!response.ok) throw new Error('Download failed');

			const blob = await response.blob();
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = $selectedItem.filename;
			a.click();
			URL.revokeObjectURL(url);
		} catch (error) {
			console.error('Download failed:', error);
			alert('Download failed. Please try again.');
		}
	}

	async function handleCopyUrl() {
		if (!$selectedItem) return;
		const fullUrl = window.location.origin + $selectedItem.url;
		try {
			await navigator.clipboard.writeText(fullUrl);
			copied = true;
			setTimeout(() => copied = false, 2000);
		} catch (error) {
			console.error('Failed to copy URL:', error);
		}
	}

	function handleEdit() {
		if (!$selectedItem || $selectedItem.type !== 'image') return;
		canvas.setEditingItem($selectedItem);
		canvas.setView('edit');
	}

	async function handleDelete() {
		if (!$selectedItem) return;
		if (confirm('Are you sure you want to delete this item?')) {
			await canvas.deleteItem($selectedItem.id);
		}
	}

	function handleBackdropClick(event: MouseEvent) {
		// Only close if clicking the backdrop itself, not the content
		if (event.target === event.currentTarget) {
			handleClose();
		}
	}

	function handleKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			handleClose();
		}
	}

	function formatDate(dateStr: string): string {
		const date = new Date(dateStr);
		return date.toLocaleDateString('en-US', {
			year: 'numeric',
			month: 'short',
			day: 'numeric',
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	function getProviderDisplayName(provider: string): string {
		switch (provider) {
			case 'google-gemini':
				return 'Nano Banana (Gemini)';
			case 'google-imagen':
				return 'Imagen 4';
			case 'openai-gpt-image':
				return 'GPT Image';
			case 'google-veo':
				return 'Veo';
			case 'openai-sora':
				return 'Sora';
			default:
				return provider;
		}
	}

	function formatFileSize(bytes: number | undefined): string {
		if (!bytes) return 'Unknown';
		if (bytes < 1024) return `${bytes} B`;
		if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
		return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
	}
</script>

<svelte:window on:keydown={handleKeydown} />

{#if $selectedItem}
	<!-- Backdrop -->
	<div
		class="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4"
		onclick={handleBackdropClick}
		role="dialog"
		aria-modal="true"
		aria-labelledby="preview-title"
	>
		<!-- Modal Container -->
		<div
			class="relative bg-card rounded-2xl border border-border shadow-2xl max-w-6xl w-full max-h-[90vh] overflow-hidden flex flex-col lg:flex-row animate-modal-in"
			onclick={(e) => e.stopPropagation()}
		>
			<!-- Close Button -->
			<button
				onclick={handleClose}
				class="absolute top-4 right-4 z-10 p-2 rounded-lg bg-black/50 text-white hover:bg-black/70 transition-colors"
				aria-label="Close preview"
			>
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
				</svg>
			</button>

			<!-- Media Display -->
			<div class="flex-1 min-h-0 bg-black flex items-center justify-center p-4 lg:p-8">
				{#if $selectedItem.type === 'image'}
					<img
						src={$selectedItem.url}
						alt={$selectedItem.prompt}
						class="max-w-full max-h-[60vh] lg:max-h-[80vh] object-contain rounded-lg"
					/>
				{:else}
					<video
						src={$selectedItem.url}
						controls
						autoplay
						loop
						class="max-w-full max-h-[60vh] lg:max-h-[80vh] object-contain rounded-lg"
					>
						<track kind="captions" />
					</video>
				{/if}
			</div>

			<!-- Info Panel -->
			<div class="lg:w-80 shrink-0 border-t lg:border-t-0 lg:border-l border-border bg-card overflow-y-auto">
				<div class="p-4 space-y-4">
					<!-- Title -->
					<div>
						<h3 id="preview-title" class="text-sm font-medium text-foreground mb-1">
							{$selectedItem.type === 'image' ? 'Image' : 'Video'} Details
						</h3>
						<p class="text-xs text-muted-foreground">{$selectedItem.filename}</p>
					</div>

					<!-- Prompt -->
					<div>
						<label class="text-xs font-medium text-muted-foreground mb-1 block">Prompt</label>
						<p class="text-sm text-foreground bg-muted/50 rounded-lg p-3 border border-border">
							{$selectedItem.prompt}
						</p>
					</div>

					<!-- Settings -->
					<div>
						<label class="text-xs font-medium text-muted-foreground mb-2 block">Settings</label>
						<div class="space-y-2 text-sm">
							<div class="flex justify-between">
								<span class="text-muted-foreground">Provider</span>
								<span class="text-foreground">{getProviderDisplayName($selectedItem.provider)}</span>
							</div>
							{#if $selectedItem.model}
								<div class="flex justify-between">
									<span class="text-muted-foreground">Model</span>
									<span class="text-foreground">{$selectedItem.model}</span>
								</div>
							{/if}
							{#if $selectedItem.settings?.aspectRatio}
								<div class="flex justify-between">
									<span class="text-muted-foreground">Aspect Ratio</span>
									<span class="text-foreground">{$selectedItem.settings.aspectRatio}</span>
								</div>
							{/if}
							{#if $selectedItem.settings?.resolution}
								<div class="flex justify-between">
									<span class="text-muted-foreground">Resolution</span>
									<span class="text-foreground">{$selectedItem.settings.resolution}</span>
								</div>
							{/if}
							{#if $selectedItem.settings?.duration}
								<div class="flex justify-between">
									<span class="text-muted-foreground">Duration</span>
									<span class="text-foreground">{$selectedItem.settings.duration}s</span>
								</div>
							{/if}
							{#if $selectedItem.fileSize}
								<div class="flex justify-between">
									<span class="text-muted-foreground">File Size</span>
									<span class="text-foreground">{formatFileSize($selectedItem.fileSize)}</span>
								</div>
							{/if}
						</div>
					</div>

					<!-- Created Date -->
					<div>
						<label class="text-xs font-medium text-muted-foreground mb-1 block">Created</label>
						<p class="text-sm text-foreground">{formatDate($selectedItem.createdAt)}</p>
					</div>

					<!-- Action Buttons -->
					<div class="pt-4 border-t border-border space-y-2">
						<button
							onclick={handleDownload}
							class="w-full px-4 py-2.5 text-sm font-medium bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors flex items-center justify-center gap-2"
						>
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
							</svg>
							Download
						</button>

						<button
							onclick={handleCopyUrl}
							class="w-full px-4 py-2.5 text-sm font-medium bg-muted text-foreground border border-border rounded-lg hover:bg-accent transition-colors flex items-center justify-center gap-2"
						>
							{#if copied}
								<svg class="w-4 h-4 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
								</svg>
								Copied!
							{:else}
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 5H6a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2v-1M8 5a2 2 0 002 2h2a2 2 0 002-2M8 5a2 2 0 012-2h2a2 2 0 012 2m0 0h2a2 2 0 012 2v3m2 4H10m0 0l3-3m-3 3l3 3" />
								</svg>
								Copy URL
							{/if}
						</button>

						{#if $selectedItem.type === 'image'}
							<button
								onclick={handleEdit}
								class="w-full px-4 py-2.5 text-sm font-medium bg-muted text-foreground border border-border rounded-lg hover:bg-accent transition-colors flex items-center justify-center gap-2"
							>
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
								</svg>
								Edit Image
							</button>
						{/if}

						<button
							onclick={handleDelete}
							disabled={$isLoading}
							class="w-full px-4 py-2.5 text-sm font-medium bg-destructive/10 text-destructive border border-destructive/30 rounded-lg hover:bg-destructive/20 transition-colors flex items-center justify-center gap-2 disabled:opacity-50"
						>
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
							</svg>
							Delete
						</button>
					</div>
				</div>
			</div>
		</div>
	</div>
{/if}

<style>
	@keyframes modal-in {
		from {
			opacity: 0;
			transform: scale(0.95);
		}
		to {
			opacity: 1;
			transform: scale(1);
		}
	}

	.animate-modal-in {
		animation: modal-in 200ms cubic-bezier(0.16, 1, 0.3, 1);
	}
</style>
