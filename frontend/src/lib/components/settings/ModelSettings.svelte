<script lang="ts">
	/**
	 * ModelSettings - Image, Video, and 3D model selection
	 */
	import '../deck/cards/card-design-system.css';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount } from 'svelte';
	import {
		ALL_IMAGE_MODELS,
		ALL_VIDEO_MODELS,
		PROVIDER_DISPLAY_NAMES
	} from '$lib/types/ai-models';

	// Image
	let imageModel = $state('');
	let selectedImageModel = $state('');
	let updatingImageModel = $state(false);
	let imageConfigSuccess = $state('');
	let imageConfigError = $state('');
	let imageExpanded = $state(false);

	// Video
	let videoModel = $state('');
	let selectedVideoModel = $state('');
	let savingVideoModel = $state(false);
	let videoConfigSuccess = $state('');
	let videoConfigError = $state('');
	let videoExpanded = $state(false);

	// 3D
	let selected3DModel = $state('meshy-6');
	let current3DModel = $state('meshy-6');
	let saving3DModel = $state(false);
	let model3DSuccess = $state('');
	let model3DError = $state('');
	let model3DExpanded = $state(false);

	onMount(() => {
		loadModelSettings();
	});

	async function loadModelSettings() {
		try {
			const settings = await api.get<{
				image_model: string | null;
				video_model: string | null;
				model_3d_model: string | null;
			}>('/settings/integrations');

			imageModel = settings.image_model || '';
			selectedImageModel = settings.image_model || '';
			videoModel = settings.video_model || '';
			selectedVideoModel = settings.video_model || '';
			selected3DModel = settings.model_3d_model || 'meshy-6';
			current3DModel = selected3DModel;
		} catch (e) {
			console.error('Failed to load model settings:', e);
		}
	}

	function groupModelsByProvider<T extends { provider: string }>(models: T[]): Record<string, T[]> {
		const groups: Record<string, T[]> = {};
		for (const model of models) {
			const name = PROVIDER_DISPLAY_NAMES[model.provider] || model.provider;
			if (!groups[name]) groups[name] = [];
			groups[name].push(model);
		}
		return groups;
	}

	async function updateImageModel() {
		if (!selectedImageModel || selectedImageModel === imageModel) return;
		updatingImageModel = true;
		imageConfigError = '';
		imageConfigSuccess = '';
		try {
			await api.patch<{success: boolean, model: string}>('/settings/integrations/image/model', {
				model: selectedImageModel
			});
			imageModel = selectedImageModel;
			imageConfigSuccess = 'Updated';
			setTimeout(() => imageConfigSuccess = '', 3000);
		} catch (e: unknown) {
			imageConfigError = getErrorMessage(e, 'Failed to update');
		} finally {
			updatingImageModel = false;
		}
	}

	async function updateVideoModel() {
		if (!selectedVideoModel || selectedVideoModel === videoModel) return;
		savingVideoModel = true;
		videoConfigError = '';
		videoConfigSuccess = '';
		try {
			await api.patch<{success: boolean, model: string}>('/settings/integrations/video/model', {
				model: selectedVideoModel
			});
			videoModel = selectedVideoModel;
			videoConfigSuccess = 'Updated';
			setTimeout(() => videoConfigSuccess = '', 3000);
		} catch (e: unknown) {
			videoConfigError = getErrorMessage(e, 'Failed to update');
		} finally {
			savingVideoModel = false;
		}
	}

	async function save3DModel() {
		if (selected3DModel === current3DModel) return;
		saving3DModel = true;
		model3DError = '';
		model3DSuccess = '';
		try {
			await api.put('/meshy/config/settings', { model: selected3DModel });
			current3DModel = selected3DModel;
			model3DSuccess = 'Updated';
			setTimeout(() => model3DSuccess = '', 3000);
		} catch (e: unknown) {
			model3DError = getErrorMessage(e, 'Failed to save');
		}
		saving3DModel = false;
	}
</script>

<!-- Image Generation -->
<section class="section">
	<button class="section-header" onclick={() => imageExpanded = !imageExpanded}>
		<h3>Image Generation</h3>
		<svg class="chevron" class:expanded={imageExpanded} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if imageExpanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<div class="model-selector">
				<select
					bind:value={selectedImageModel}
					onchange={updateImageModel}
					disabled={updatingImageModel}
					class="input select"
				>
					{#each Object.entries(groupModelsByProvider(ALL_IMAGE_MODELS)) as [provider, models] (provider)}
						<optgroup label={provider}>
							{#each models as model (model.id)}
								<option value={model.id}>
									{model.displayName} — ${model.pricePerImage.toFixed(2)}/img
								</option>
							{/each}
						</optgroup>
					{/each}
				</select>
				{#if updatingImageModel}
					<span class="loading-indicator">Updating...</span>
				{/if}
			</div>
			{#if imageConfigSuccess}<div class="success-msg">{imageConfigSuccess}</div>{/if}
			{#if imageConfigError}<div class="error-msg">{imageConfigError}</div>{/if}
		</div>
	{/if}
</section>

<!-- Video Generation -->
<section class="section">
	<button class="section-header" onclick={() => videoExpanded = !videoExpanded}>
		<h3>Video Generation</h3>
		<svg class="chevron" class:expanded={videoExpanded} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if videoExpanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<div class="model-selector">
				<select
					bind:value={selectedVideoModel}
					onchange={updateVideoModel}
					disabled={savingVideoModel}
					class="input select"
				>
					{#each Object.entries(groupModelsByProvider(ALL_VIDEO_MODELS)) as [provider, models] (provider)}
						<optgroup label={provider}>
							{#each models as model (model.id)}
								<option value={model.id}>
									{model.displayName} — ${model.pricePerSecond.toFixed(2)}/sec, max {model.maxDuration}s
								</option>
							{/each}
						</optgroup>
					{/each}
				</select>
				{#if savingVideoModel}
					<span class="loading-indicator">Updating...</span>
				{/if}
			</div>
			{#if videoConfigSuccess}<div class="success-msg">{videoConfigSuccess}</div>{/if}
			{#if videoConfigError}<div class="error-msg">{videoConfigError}</div>{/if}
		</div>
	{/if}
</section>

<!-- 3D Generation -->
<section class="section">
	<button class="section-header" onclick={() => model3DExpanded = !model3DExpanded}>
		<h3>3D Generation</h3>
		<svg class="chevron" class:expanded={model3DExpanded} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if model3DExpanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<div class="model-selector">
				<select
					bind:value={selected3DModel}
					onchange={save3DModel}
					disabled={saving3DModel}
					class="input select"
				>
					<option value="meshy-6">Meshy 6 (Latest) — Best Quality</option>
					<option value="meshy-5">Meshy 5 — Balanced</option>
					<option value="meshy-4">Meshy 4 — Fast</option>
				</select>
				{#if saving3DModel}
					<span class="loading-indicator">Updating...</span>
				{/if}
			</div>
			{#if model3DSuccess}<div class="success-msg">{model3DSuccess}</div>{/if}
			{#if model3DError}<div class="error-msg">{model3DError}</div>{/if}
		</div>
	{/if}
</section>

<style>
	.section {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.section-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 12px 16px;
		background: transparent;
		border: none;
		cursor: pointer;
		transition: background 150ms ease;
	}

	.section-header:hover { background: var(--hover-overlay); }
	.section-header:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.section-header h3 {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
	}

	@media (min-width: 640px) {
		.section-header {
			justify-content: center;
			position: relative;
		}
		.section-header .chevron {
			position: absolute;
			right: 16px;
		}
	}

	.chevron {
		width: 20px;
		height: 20px;
		color: var(--muted-foreground);
		transition: transform 200ms ease;
	}

	.chevron.expanded { transform: rotate(180deg); }

	.section-content {
		padding: 8px 16px 16px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.model-selector {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.model-selector .input { flex: 1; }

	.input {
		padding: 8px 12px;
		background-color: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		color: var(--foreground);
		font-size: 0.875rem;
		transition: border-color 150ms ease, box-shadow 150ms ease;
	}

	.input:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px var(--primary-20);
	}

	.loading-indicator {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.success-msg {
		font-size: 0.8125rem;
		color: var(--success);
	}

	.error-msg {
		font-size: 0.8125rem;
		color: var(--destructive);
	}
</style>
