<script lang="ts">
	/**
	 * CanvasCard - Media generation card for The Deck
	 *
	 * Features:
	 * - Large preview area
	 * - Prompt input
	 * - Provider selector dropdown
	 * - Aspect ratio buttons
	 * - Generate button
	 * - Loading state with progress
	 */

	import { Image, Loader2, Sparkles, ChevronDown, Check } from 'lucide-svelte';
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';

	interface Provider {
		id: string;
		name: string;
		icon?: string;
	}

	interface Props {
		card: DeckCard;
		canvasId: string;
		onClose: () => void;
		onMinimize: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		onGenerate?: (prompt: string, provider: string, aspectRatio: string) => void;
	}

	let {
		card,
		canvasId,
		onClose,
		onMinimize,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		onGenerate
	}: Props = $props();

	// Form state
	let prompt = $state('');
	let selectedProvider = $state('openai');
	let selectedAspectRatio = $state('16:9');
	let showProviderDropdown = $state(false);

	// Generation state
	let isGenerating = $state(false);
	let progress = $state(0);
	let previewUrl = $state<string | null>(null);
	let error = $state<string | null>(null);

	// Mock providers (would come from API)
	const providers: Provider[] = [
		{ id: 'openai', name: 'DALL-E 3' },
		{ id: 'stability', name: 'Stable Diffusion' },
		{ id: 'midjourney', name: 'Midjourney' },
		{ id: 'google', name: 'Imagen' },
	];

	const aspectRatios = ['1:1', '16:9', '9:16', '4:3', '3:4'];

	// Selected provider object
	const currentProvider = $derived(providers.find((p) => p.id === selectedProvider) || providers[0]);

	// Get aspect ratio preview dimensions
	function getAspectRatioPreview(ratio: string): { width: number; height: number } {
		const [w, h] = ratio.split(':').map(Number);
		const maxSize = 24;
		if (w > h) {
			return { width: maxSize, height: Math.round((maxSize * h) / w) };
		} else {
			return { width: Math.round((maxSize * w) / h), height: maxSize };
		}
	}

	// Handle provider selection
	function selectProvider(providerId: string) {
		selectedProvider = providerId;
		showProviderDropdown = false;
	}

	// Handle generation
	async function handleGenerate() {
		if (!prompt.trim() || isGenerating) return;

		isGenerating = true;
		progress = 0;
		error = null;

		// Simulate generation progress
		const progressInterval = setInterval(() => {
			progress = Math.min(progress + Math.random() * 15, 90);
		}, 500);

		try {
			// In real implementation, this would call the API
			onGenerate?.(prompt, selectedProvider, selectedAspectRatio);

			// Simulate API delay
			await new Promise((resolve) => setTimeout(resolve, 3000));

			progress = 100;

			// Mock result (would be actual URL from API)
			previewUrl = `https://via.placeholder.com/600x400/1a1a2e/ffffff?text=Generated+Image`;
		} catch (err) {
			error = 'Failed to generate image. Please try again.';
		} finally {
			clearInterval(progressInterval);
			isGenerating = false;
		}
	}

	// Close provider dropdown on outside click
	function handleCardClick(e: MouseEvent) {
		const target = e.target as HTMLElement;
		if (!target.closest('.provider-selector')) {
			showProviderDropdown = false;
		}
	}
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<BaseCard {card} {onClose} {onMinimize} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
	<div class="canvas-content" onclick={handleCardClick}>
		<!-- Preview Area -->
		<div class="preview-area">
			{#if isGenerating}
				<div class="preview-loading">
					<Loader2 size={32} class="animate-spin" />
					<span class="loading-text">Generating...</span>
					<div class="progress-bar-container">
						<div class="progress-bar" style:width="{progress}%"></div>
					</div>
				</div>
			{:else if previewUrl}
				<img src={previewUrl} alt="Generated" class="preview-image" />
			{:else if error}
				<div class="preview-error">
					<span>{error}</span>
				</div>
			{:else}
				<div class="preview-placeholder">
					<Image size={48} strokeWidth={1} />
					<span>Your generated image will appear here</span>
				</div>
			{/if}
		</div>

		<!-- Controls Area -->
		<div class="controls-area">
			<!-- Prompt Input -->
			<div class="prompt-container">
				<textarea
					bind:value={prompt}
					placeholder="Describe the image you want to generate..."
					rows="2"
					class="prompt-input"
					disabled={isGenerating}
				></textarea>
			</div>

			<!-- Options Row -->
			<div class="options-row">
				<!-- Provider Selector -->
				<div class="provider-selector">
					<button
						class="provider-btn"
						onclick={() => (showProviderDropdown = !showProviderDropdown)}
						disabled={isGenerating}
					>
						<span>{currentProvider.name}</span>
						<ChevronDown size={14} />
					</button>

					{#if showProviderDropdown}
						<div class="provider-dropdown">
							{#each providers as provider}
								<button
									class="provider-option"
									class:selected={provider.id === selectedProvider}
									onclick={() => selectProvider(provider.id)}
								>
									<span>{provider.name}</span>
									{#if provider.id === selectedProvider}
										<Check size={14} />
									{/if}
								</button>
							{/each}
						</div>
					{/if}
				</div>

				<!-- Aspect Ratio Buttons -->
				<div class="aspect-ratios">
					{#each aspectRatios as ratio}
						{@const preview = getAspectRatioPreview(ratio)}
						<button
							class="aspect-btn"
							class:selected={ratio === selectedAspectRatio}
							onclick={() => (selectedAspectRatio = ratio)}
							disabled={isGenerating}
							title={ratio}
						>
							<div
								class="aspect-preview"
								style:width="{preview.width}px"
								style:height="{preview.height}px"
							></div>
						</button>
					{/each}
				</div>
			</div>

			<!-- Generate Button -->
			<button
				class="generate-btn"
				onclick={handleGenerate}
				disabled={!prompt.trim() || isGenerating}
			>
				{#if isGenerating}
					<Loader2 size={16} class="animate-spin" />
					<span>Generating...</span>
				{:else}
					<Sparkles size={16} />
					<span>Generate</span>
				{/if}
			</button>
		</div>
	</div>
</BaseCard>

<style>
	.canvas-content {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	/* Preview Area */
	.preview-area {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		background: hsl(var(--muted) / 0.3);
		border-bottom: 1px solid hsl(var(--border) / 0.5);
		min-height: 200px;
		overflow: hidden;
	}

	.preview-placeholder {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 12px;
		color: hsl(var(--muted-foreground));
		text-align: center;
		padding: 24px;
	}

	.preview-placeholder span {
		font-size: 0.8125rem;
	}

	.preview-loading {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 12px;
		color: hsl(var(--primary));
	}

	.loading-text {
		font-size: 0.8125rem;
		color: hsl(var(--muted-foreground));
	}

	.progress-bar-container {
		width: 150px;
		height: 4px;
		background: hsl(var(--muted));
		border-radius: 2px;
		overflow: hidden;
	}

	.progress-bar {
		height: 100%;
		background: hsl(var(--primary));
		transition: width 0.3s ease;
	}

	.preview-image {
		max-width: 100%;
		max-height: 100%;
		object-fit: contain;
	}

	.preview-error {
		color: hsl(var(--destructive));
		font-size: 0.8125rem;
		padding: 24px;
		text-align: center;
	}

	/* Controls Area */
	.controls-area {
		padding: 12px;
		display: flex;
		flex-direction: column;
		gap: 10px;
		flex-shrink: 0;
	}

	.prompt-container {
		width: 100%;
	}

	.prompt-input {
		width: 100%;
		padding: 10px 12px;
		background: hsl(var(--muted));
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
		font-size: 0.8125rem;
		color: hsl(var(--foreground));
		resize: none;
		outline: none;
		transition: border-color 0.15s ease;
	}

	.prompt-input::placeholder {
		color: hsl(var(--muted-foreground));
	}

	.prompt-input:focus {
		border-color: hsl(var(--primary) / 0.5);
	}

	.prompt-input:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Options Row */
	.options-row {
		display: flex;
		align-items: center;
		gap: 12px;
		justify-content: space-between;
	}

	/* Provider Selector */
	.provider-selector {
		position: relative;
	}

	.provider-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 6px 10px;
		background: hsl(var(--muted));
		border: 1px solid hsl(var(--border));
		border-radius: 6px;
		font-size: 0.75rem;
		color: hsl(var(--foreground));
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.provider-btn:hover:not(:disabled) {
		background: hsl(var(--accent));
	}

	.provider-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.provider-dropdown {
		position: absolute;
		bottom: 100%;
		left: 0;
		margin-bottom: 4px;
		min-width: 140px;
		background: hsl(var(--popover));
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
		padding: 4px;
		z-index: 100;
	}

	.provider-option {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 8px 10px;
		background: transparent;
		border: none;
		border-radius: 4px;
		font-size: 0.75rem;
		color: hsl(var(--foreground));
		cursor: pointer;
		transition: background 0.1s ease;
	}

	.provider-option:hover {
		background: hsl(var(--accent));
	}

	.provider-option.selected {
		color: hsl(var(--primary));
	}

	/* Aspect Ratios */
	.aspect-ratios {
		display: flex;
		gap: 4px;
	}

	.aspect-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		background: hsl(var(--muted));
		border: 1px solid hsl(var(--border));
		border-radius: 6px;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.aspect-btn:hover:not(:disabled) {
		background: hsl(var(--accent));
	}

	.aspect-btn.selected {
		border-color: hsl(var(--primary));
		background: hsl(var(--primary) / 0.1);
	}

	.aspect-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.aspect-preview {
		background: hsl(var(--muted-foreground) / 0.4);
		border-radius: 2px;
	}

	.aspect-btn.selected .aspect-preview {
		background: hsl(var(--primary));
	}

	/* Generate Button */
	.generate-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 10px 20px;
		background: hsl(var(--primary));
		border: none;
		border-radius: 8px;
		font-size: 0.875rem;
		font-weight: 500;
		color: hsl(var(--primary-foreground));
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.generate-btn:hover:not(:disabled) {
		opacity: 0.9;
	}

	.generate-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Utility */
	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from {
			transform: rotate(0deg);
		}
		to {
			transform: rotate(360deg);
		}
	}
</style>
