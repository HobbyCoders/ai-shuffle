<script lang="ts">
	/**
	 * ModelSettings — Dynamic model selection + per-model settings
	 *
	 * Shows model selection dropdowns and dynamic settings panels
	 * that display model-specific options (aspect ratios, resolutions,
	 * durations) based on the currently selected model.
	 */
	import '../deck/cards/card-design-system.css';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount } from 'svelte';
	import ModelBrowser from './ModelBrowser.svelte';
	import {
		ALL_IMAGE_MODELS,
		ALL_VIDEO_MODELS,
		ALL_3D_MODELS,
		TEXT_TO_VIDEO_MODELS,
		IMAGE_TO_VIDEO_MODELS,
		getImageModel,
		getVideoModel,
	} from '$lib/types/ai-models';
	import type { CatalogModel, ImageModel, VideoModel } from '$lib/types/ai-models';

	// Current model selections
	let imageModel = $state('');
	let videoModel = $state('');
	let model3D = $state('');

	// Per-model settings
	let imageAspectRatio = $state('');
	let imageResolution = $state('');
	let videoAspectRatio = $state('');
	let videoDuration = $state<number | null>(null);
	let videoResolution = $state('');

	// Status
	let successMsg = $state('');
	let errorMsg = $state('');
	let saving = $state(false);

	// Section expansion state
	let browserExpanded = $state(false);
	let imageExpanded = $state(false);
	let videoExpanded = $state(false);
	let model3DExpanded = $state(false);

	// Derived: current model metadata
	let currentImageModel = $derived<ImageModel | undefined>(getImageModel(imageModel));
	let currentVideoModel = $derived<VideoModel | undefined>(getVideoModel(videoModel));

	let selectedModels = $derived<Record<string, string>>({
		image_generation: imageModel,
		video_generation: videoModel,
		text_to_3d: model3D,
	});

	onMount(() => {
		loadModelSettings();
	});

	async function loadModelSettings() {
		try {
			const settings = await api.get<{
				image_model: string | null;
				video_model: string | null;
				model3d_model: string | null;
				image_aspect_ratio: string | null;
				image_resolution: string | null;
				video_aspect_ratio: string | null;
				video_duration: number | null;
				video_resolution: string | null;
			}>('/settings/integrations');

			imageModel = settings.image_model || 'fal-ai/flux-pro/v1.1-ultra';
			videoModel = settings.video_model || 'fal-ai/kling-video/o3/pro/text-to-video';
			model3D = settings.model3d_model || 'fal-ai/meshy/text-to-3d';
			imageAspectRatio = settings.image_aspect_ratio || '';
			imageResolution = settings.image_resolution || '';
			videoAspectRatio = settings.video_aspect_ratio || '';
			videoDuration = settings.video_duration || null;
			videoResolution = settings.video_resolution || '';
		} catch (e) {
			console.error('Failed to load model settings:', e);
		}
	}

	function showSuccess(msg: string) {
		successMsg = msg;
		setTimeout(() => successMsg = '', 2000);
	}

	// ---- Image settings ----
	async function updateImageModel(e: Event) {
		const select = e.target as HTMLSelectElement;
		const modelId = select.value;
		if (!modelId || modelId === imageModel) return;
		saving = true;
		errorMsg = '';
		try {
			await api.patch<{success: boolean}>('/settings/integrations/image/model', { model: modelId });
			imageModel = modelId;
			// Reset settings to defaults for new model
			imageAspectRatio = '';
			imageResolution = '';
			await api.patch('/settings/integrations/image/settings', { aspect_ratio: '', resolution: '' });
			showSuccess('Updated');
		} catch (e: unknown) {
			errorMsg = getErrorMessage(e, 'Failed to update');
		} finally {
			saving = false;
		}
	}

	async function setImageAspectRatio(ratio: string) {
		if (ratio === imageAspectRatio) return;
		imageAspectRatio = ratio;
		try {
			await api.patch('/settings/integrations/image/settings', { aspect_ratio: ratio });
		} catch (e: unknown) {
			errorMsg = getErrorMessage(e, 'Failed to save');
		}
	}

	async function setImageResolution(res: string) {
		if (res === imageResolution) return;
		imageResolution = res;
		try {
			await api.patch('/settings/integrations/image/settings', { resolution: res });
		} catch (e: unknown) {
			errorMsg = getErrorMessage(e, 'Failed to save');
		}
	}

	// ---- Video settings ----
	async function updateVideoModel(e: Event) {
		const select = e.target as HTMLSelectElement;
		const modelId = select.value;
		if (!modelId || modelId === videoModel) return;
		saving = true;
		errorMsg = '';
		try {
			await api.patch<{success: boolean}>('/settings/integrations/video/model', { model: modelId });
			videoModel = modelId;
			// Reset settings
			videoAspectRatio = '';
			videoDuration = null;
			videoResolution = '';
			await api.patch('/settings/integrations/video/settings', { aspect_ratio: '', duration: 0, resolution: '' });
			showSuccess('Updated');
		} catch (e: unknown) {
			errorMsg = getErrorMessage(e, 'Failed to update');
		} finally {
			saving = false;
		}
	}

	async function setVideoAspectRatio(ratio: string) {
		if (ratio === videoAspectRatio) return;
		videoAspectRatio = ratio;
		try {
			await api.patch('/settings/integrations/video/settings', { aspect_ratio: ratio });
		} catch (e: unknown) {
			errorMsg = getErrorMessage(e, 'Failed to save');
		}
	}

	async function setVideoDuration(dur: number) {
		if (dur === videoDuration) return;
		videoDuration = dur;
		try {
			await api.patch('/settings/integrations/video/settings', { duration: dur });
		} catch (e: unknown) {
			errorMsg = getErrorMessage(e, 'Failed to save');
		}
	}

	async function setVideoResolution(res: string) {
		if (res === videoResolution) return;
		videoResolution = res;
		try {
			await api.patch('/settings/integrations/video/settings', { resolution: res });
		} catch (e: unknown) {
			errorMsg = getErrorMessage(e, 'Failed to save');
		}
	}

	// ---- 3D + Browser ----
	async function update3DModel(e: Event) {
		const select = e.target as HTMLSelectElement;
		const modelId = select.value;
		if (!modelId || modelId === model3D) return;
		saving = true;
		errorMsg = '';
		try {
			await api.patch<{success: boolean}>('/settings/integrations/3d/model', { model: modelId });
			model3D = modelId;
			showSuccess('Updated');
		} catch (e: unknown) {
			errorMsg = getErrorMessage(e, 'Failed to update');
		} finally {
			saving = false;
		}
	}

	async function setDefaultModel(model: CatalogModel, toolType: string) {
		saving = true;
		errorMsg = '';
		successMsg = '';
		let endpoint = '';
		if (toolType === 'image_generation' || toolType === 'image_editing') {
			endpoint = '/settings/integrations/image/model';
		} else if (toolType === 'video_generation' || toolType === 'image_to_video') {
			endpoint = '/settings/integrations/video/model';
		} else if (toolType === 'text_to_3d' || toolType === 'image_to_3d') {
			endpoint = '/settings/integrations/3d/model';
		} else {
			errorMsg = `No settings endpoint for tool type: ${toolType}`;
			saving = false;
			return;
		}
		try {
			await api.patch<{ success: boolean }>(endpoint, { model: model.endpoint_id });
			if (toolType === 'image_generation' || toolType === 'image_editing') {
				imageModel = model.endpoint_id;
			} else if (toolType === 'video_generation' || toolType === 'image_to_video') {
				videoModel = model.endpoint_id;
			} else if (toolType === 'text_to_3d' || toolType === 'image_to_3d') {
				model3D = model.endpoint_id;
			}
			showSuccess(`Default set: ${model.display_name}`);
		} catch (e) {
			errorMsg = getErrorMessage(e, 'Failed to set default model');
		} finally {
			saving = false;
		}
	}
</script>

<!-- Status messages -->
{#if successMsg}<div class="success-msg">{successMsg}</div>{/if}
{#if errorMsg}<div class="error-msg">{errorMsg}</div>{/if}

<!-- Current Defaults Summary -->
<section class="section defaults-summary">
	<div class="defaults-grid">
		<div class="default-item">
			<span class="default-label">Image</span>
			<span class="default-value">{imageModel || 'Not set'}</span>
		</div>
		<div class="default-item">
			<span class="default-label">Video</span>
			<span class="default-value">{videoModel || 'Not set'}</span>
		</div>
		<div class="default-item">
			<span class="default-label">3D</span>
			<span class="default-value">{model3D || 'Not set'}</span>
		</div>
	</div>
</section>

<!-- Model Catalog Browser -->
<section class="section">
	<button class="section-header" onclick={() => browserExpanded = !browserExpanded}>
		<h3>Browse All Models</h3>
		<span class="badge">600+</span>
		<svg class="chevron" class:expanded={browserExpanded} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if browserExpanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<p class="section-hint">Click any model to set it as the default for its category. New models from fal.ai appear automatically.</p>
			<ModelBrowser
				{selectedModels}
				onselect={setDefaultModel}
			/>
		</div>
	{/if}
</section>

<!-- Quick Select: Image + Settings -->
<section class="section">
	<button class="section-header" onclick={() => imageExpanded = !imageExpanded}>
		<h3>Image</h3>
		<svg class="chevron" class:expanded={imageExpanded} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if imageExpanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<div class="model-selector">
				<select
					value={imageModel}
					onchange={updateImageModel}
					disabled={saving}
					class="input select"
				>
					{#each ALL_IMAGE_MODELS as model (model.id)}
						<option value={model.id}>
							{model.displayName} — ${model.pricePerImage.toFixed(3)}/img
						</option>
					{/each}
				</select>
			</div>
			{#if currentImageModel}
				<p class="model-description">{currentImageModel.description}</p>

				<!-- Per-model settings -->
				<div class="settings-panel">
					<!-- Aspect Ratio -->
					<div class="setting-group">
						<span class="setting-label">Aspect Ratio</span>
						<div class="pill-group">
							{#each currentImageModel.aspectRatios as ratio}
								<button
									class="pill"
									class:active={imageAspectRatio === ratio}
									onclick={() => setImageAspectRatio(ratio)}
								>{ratio}</button>
							{/each}
						</div>
					</div>

					<!-- Resolution -->
					{#if currentImageModel.resolutions.length > 0}
						<div class="setting-group">
							<span class="setting-label">Resolution</span>
							<div class="pill-group">
								{#each currentImageModel.resolutions as res}
									<button
										class="pill"
										class:active={imageResolution === res}
										onclick={() => setImageResolution(res)}
									>{res}</button>
								{/each}
							</div>
						</div>
					{/if}

					<!-- Capabilities -->
					<div class="capabilities">
						{#if currentImageModel.capabilities.editing}
							<span class="cap-badge cap-green">Editing</span>
						{/if}
						{#if currentImageModel.capabilities.referenceImages}
							<span class="cap-badge cap-blue">Ref Images (max {currentImageModel.capabilities.maxReferenceImages})</span>
						{/if}
						{#if currentImageModel.capabilities.textRendering !== 'none'}
							<span class="cap-badge cap-purple">Text: {currentImageModel.capabilities.textRendering}</span>
						{/if}
					</div>
				</div>
			{/if}
		</div>
	{/if}
</section>

<!-- Quick Select: Video + Settings -->
<section class="section">
	<button class="section-header" onclick={() => videoExpanded = !videoExpanded}>
		<h3>Video</h3>
		<svg class="chevron" class:expanded={videoExpanded} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if videoExpanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<div class="model-selector">
				<select
					value={videoModel}
					onchange={updateVideoModel}
					disabled={saving}
					class="input select"
				>
					<optgroup label="Text-to-Video">
						{#each TEXT_TO_VIDEO_MODELS as model (model.id)}
							<option value={model.id}>
								{model.displayName} — ${model.pricePerSecond.toFixed(2)}/sec
							</option>
						{/each}
					</optgroup>
					<optgroup label="Image-to-Video">
						{#each IMAGE_TO_VIDEO_MODELS as model (model.id)}
							<option value={model.id}>
								{model.displayName} — ${model.pricePerSecond.toFixed(2)}/sec
							</option>
						{/each}
					</optgroup>
				</select>
			</div>
			{#if currentVideoModel}
				<p class="model-description">{currentVideoModel.description}</p>

				<!-- Per-model settings -->
				<div class="settings-panel">
					<!-- Native Audio badge -->
					{#if currentVideoModel.capabilities.nativeAudio}
						<div class="capabilities" style="margin-bottom: 4px;">
							<span class="cap-badge cap-green">Native Audio</span>
						</div>
					{/if}

					<!-- Aspect Ratio -->
					<div class="setting-group">
						<span class="setting-label">Aspect Ratio</span>
						<div class="pill-group">
							{#each currentVideoModel.aspectRatios as ratio}
								<button
									class="pill"
									class:active={videoAspectRatio === ratio}
									onclick={() => setVideoAspectRatio(ratio)}
								>{ratio}</button>
							{/each}
						</div>
					</div>

					<!-- Duration -->
					{#if currentVideoModel.durations.length > 0}
						<div class="setting-group">
							<span class="setting-label">Duration</span>
							<div class="pill-group">
								{#each currentVideoModel.durations as dur}
									<button
										class="pill"
										class:active={videoDuration === dur}
										onclick={() => setVideoDuration(dur)}
									>{dur}s</button>
								{/each}
							</div>
						</div>
					{/if}

					<!-- Resolution -->
					{#if currentVideoModel.resolutions.length > 0}
						<div class="setting-group">
							<span class="setting-label">Resolution</span>
							<div class="pill-group">
								{#each currentVideoModel.resolutions as res}
									<button
										class="pill"
										class:active={videoResolution === res}
										onclick={() => setVideoResolution(res)}
									>{res}</button>
								{/each}
							</div>
						</div>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</section>

<!-- Quick Select: 3D -->
<section class="section">
	<button class="section-header" onclick={() => model3DExpanded = !model3DExpanded}>
		<h3>3D</h3>
		<svg class="chevron" class:expanded={model3DExpanded} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if model3DExpanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<div class="model-selector">
				<select
					value={model3D}
					onchange={update3DModel}
					disabled={saving}
					class="input select"
				>
					{#each ALL_3D_MODELS as model (model.id)}
						<option value={model.id}>
							{model.displayName} — ${model.pricePerModel.toFixed(2)}/model
						</option>
					{/each}
				</select>
			</div>
			{#if model3D}
				{@const model = ALL_3D_MODELS.find(m => m.id === model3D)}
				{#if model}
					<p class="model-description">{model.description}</p>
				{/if}
			{/if}
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
		gap: 8px;
	}

	.section-header:hover { background: var(--hover-overlay); }
	.section-header:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.section-header h3 {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
		flex: 1;
		text-align: left;
	}

	.badge {
		padding: 2px 8px;
		background: var(--primary);
		color: var(--primary-foreground);
		font-size: 0.6875rem;
		font-weight: 700;
		border-radius: 9999px;
		flex-shrink: 0;
	}

	.chevron {
		width: 20px;
		height: 20px;
		color: var(--muted-foreground);
		transition: transform 200ms ease;
		flex-shrink: 0;
	}

	.chevron.expanded { transform: rotate(180deg); }

	.section-content {
		padding: 8px 16px 16px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.section-hint {
		margin: 0;
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		line-height: 1.4;
	}

	.defaults-summary {
		padding: 12px 16px;
	}

	.defaults-grid {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 12px;
	}

	.default-item {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.default-label {
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.default-value {
		font-size: 0.75rem;
		color: var(--foreground);
		font-family: monospace;
		word-break: break-all;
		line-height: 1.3;
	}

	.model-selector {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.model-selector .input { flex: 1; }

	.model-description {
		margin: 0;
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		line-height: 1.4;
	}

	/* Per-model settings panel */
	.settings-panel {
		background: var(--muted);
		border-radius: calc(var(--radius) - 2px);
		padding: 12px;
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.setting-group {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.setting-label {
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.pill-group {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}

	.pill {
		padding: 4px 10px;
		font-size: 0.75rem;
		font-weight: 500;
		border: 1px solid var(--border);
		border-radius: 9999px;
		background: var(--card);
		color: var(--foreground);
		cursor: pointer;
		transition: all 150ms ease;
		min-height: 28px;
	}

	.pill:hover {
		border-color: var(--ring);
		background: var(--hover-overlay);
	}

	.pill.active {
		background: var(--primary);
		color: var(--primary-foreground);
		border-color: var(--primary);
	}

	/* Capability badges */
	.capabilities {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}

	.cap-badge {
		padding: 2px 8px;
		font-size: 0.6875rem;
		font-weight: 600;
		border-radius: 9999px;
		line-height: 1.4;
	}

	.cap-green {
		background: color-mix(in srgb, var(--success) 15%, transparent);
		color: var(--success);
	}

	.cap-blue {
		background: color-mix(in srgb, var(--ring) 15%, transparent);
		color: var(--ring);
	}

	.cap-purple {
		background: color-mix(in srgb, #a855f7 15%, transparent);
		color: #a855f7;
	}

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

	.success-msg {
		font-size: 0.8125rem;
		color: var(--success);
		padding: 8px 16px;
		background: var(--card);
		border: 1px solid var(--success);
		border-radius: var(--radius);
	}

	.error-msg {
		font-size: 0.8125rem;
		color: var(--destructive);
		padding: 8px 16px;
		background: var(--card);
		border: 1px solid var(--destructive);
		border-radius: var(--radius);
	}

	@media (max-width: 640px) {
		.defaults-grid {
			grid-template-columns: 1fr;
			gap: 8px;
		}
	}
</style>
