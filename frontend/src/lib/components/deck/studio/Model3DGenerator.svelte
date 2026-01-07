<script lang="ts">
	import { Box, Image, Palette, Bone, Play, Upload, X, ChevronDown, Sparkles } from 'lucide-svelte';
	import { studio } from '$lib/stores/studio';
	import {
		ALL_3D_MODELS,
		get3DModel,
		PROVIDER_DISPLAY_NAMES,
		type Model3DModel,
		type Model3DArtStyle,
		type Model3DTopology
	} from '$lib/types/ai-models';

	// Props
	interface Props {
		onStartGeneration?: (type: '3d', config: Model3DGenerationConfig) => void;
	}

	let { onStartGeneration }: Props = $props();

	// Types
	interface Model3DGenerationConfig {
		prompt: string;
		provider: string;
		model: string;
		mode: Model3DMode;
		artStyle: Model3DArtStyle;
		topology: Model3DTopology;
		targetPolycount: number;
		enablePbr: boolean;
		sourceImageUrl?: string;
	}

	type Model3DMode = 'text-to-3d' | 'image-to-3d' | 'retexture' | 'rig' | 'animate';

	// Generation mode
	let mode: Model3DMode = $state('text-to-3d');

	// Form state
	let prompt = $state('');
	let artStyle = $state<Model3DArtStyle>('realistic');
	let topology = $state<Model3DTopology>('triangle');
	let targetPolycount = $state(30000);
	let selectedModelId = $state('meshy-6');
	let imageFile: File | null = $state(null);
	let imagePreviewUrl: string | null = $state(null);
	let enablePbr = $state(true);
	let showAdvanced = $state(false);

	// Generation state
	let isGenerating = $state(false);
	let progress = $state(0);
	let taskId = $state<string | null>(null);

	// File input ref
	let fileInputElement: HTMLInputElement | null = $state(null);

	// Mode tabs configuration
	const modes: { id: Model3DMode; label: string; icon: typeof Box; description: string }[] = [
		{ id: 'text-to-3d', label: 'Text to 3D', icon: Box, description: 'Generate 3D models from text prompts' },
		{ id: 'image-to-3d', label: 'Image to 3D', icon: Image, description: 'Convert images to 3D models' },
		{ id: 'retexture', label: 'Retexture', icon: Palette, description: 'Apply new textures to existing models' },
		{ id: 'rig', label: 'Rig', icon: Bone, description: 'Add skeletal rigs for animation' },
		{ id: 'animate', label: 'Animate', icon: Play, description: 'Generate animations for rigged models' }
	];

	// Art style options
	const artStyles: { value: Model3DArtStyle; label: string }[] = [
		{ value: 'realistic', label: 'Realistic' },
		{ value: 'sculpture', label: 'Sculpture' }
	];

	// Topology options
	const topologies: { value: Model3DTopology; label: string }[] = [
		{ value: 'triangle', label: 'Triangle' },
		{ value: 'quad', label: 'Quad' }
	];

	// Polycount presets
	const polycountPresets = [
		{ value: 10000, label: '10K (Low)' },
		{ value: 30000, label: '30K (Medium)' },
		{ value: 100000, label: '100K (High)' },
		{ value: 300000, label: '300K (Ultra)' }
	];

	// Derived
	let currentModel = $derived(get3DModel(selectedModelId) || ALL_3D_MODELS[0]);
	let canGenerate = $derived.by(() => {
		const hasPrompt = prompt.trim().length > 0;
		const notGenerating = !isGenerating;
		const hasImageIfNeeded = mode !== 'image-to-3d' || imageFile !== null;
		return hasPrompt && notGenerating && hasImageIfNeeded;
	});

	// Check mode capabilities
	let modeSupported = $derived.by(() => {
		if (!currentModel) return false;
		switch (mode) {
			case 'text-to-3d': return currentModel.capabilities.textTo3D;
			case 'image-to-3d': return currentModel.capabilities.imageTo3D;
			case 'retexture': return currentModel.capabilities.retexture;
			case 'rig': return currentModel.capabilities.rigging;
			case 'animate': return currentModel.capabilities.animation;
			default: return false;
		}
	});

	// Get capability badges for a model
	function getModelBadges(model: Model3DModel): { label: string; color: string }[] {
		const badges: { label: string; color: string }[] = [];
		const caps = model.capabilities;

		if (caps.textTo3D) badges.push({ label: 'Text to 3D', color: 'bg-blue-500/20 text-blue-400' });
		if (caps.imageTo3D) badges.push({ label: 'Image to 3D', color: 'bg-purple-500/20 text-purple-400' });
		if (caps.retexture) badges.push({ label: 'Retexture', color: 'bg-green-500/20 text-green-400' });
		if (caps.rigging) badges.push({ label: 'Rigging', color: 'bg-orange-500/20 text-orange-400' });
		if (caps.animation) badges.push({ label: 'Animation', color: 'bg-pink-500/20 text-pink-400' });
		if (caps.pbr) badges.push({ label: 'PBR', color: 'bg-cyan-500/20 text-cyan-400' });

		return badges;
	}

	// Handlers
	function handleModelChange(modelId: string) {
		selectedModelId = modelId;
		// Could update store here if needed
	}

	function handleModeChange(newMode: Model3DMode) {
		mode = newMode;
		// Clear image if switching away from image-to-3d
		if (newMode !== 'image-to-3d' && imageFile) {
			clearImage();
		}
	}

	function handleFileUpload(event: Event) {
		const input = event.target as HTMLInputElement;
		if (!input.files?.length) return;

		const file = input.files[0];
		if (!file.type.startsWith('image/')) return;

		// Clean up previous preview
		if (imagePreviewUrl) {
			URL.revokeObjectURL(imagePreviewUrl);
		}

		imageFile = file;
		imagePreviewUrl = URL.createObjectURL(file);
		input.value = '';
	}

	function clearImage() {
		if (imagePreviewUrl) {
			URL.revokeObjectURL(imagePreviewUrl);
		}
		imageFile = null;
		imagePreviewUrl = null;
	}

	function triggerFileUpload() {
		fileInputElement?.click();
	}

	async function handleGenerate() {
		if (!canGenerate) return;

		isGenerating = true;
		progress = 0;

		try {
			const config: Model3DGenerationConfig = {
				prompt,
				provider: currentModel.provider,
				model: selectedModelId,
				mode,
				artStyle,
				topology,
				targetPolycount,
				enablePbr,
				sourceImageUrl: imagePreviewUrl || undefined
			};

			// Call the callback to trigger generation
			await onStartGeneration?.('3d', config);

			// Reset form after successful generation
			prompt = '';
			clearImage();
		} catch (error) {
			console.error('3D generation failed:', error);
		} finally {
			isGenerating = false;
			progress = 0;
		}
	}
</script>

<div class="p-4 space-y-6">
	<!-- Mode Selector Tabs -->
	<div>
		<label class="block text-xs text-muted-foreground mb-2">Generation Mode</label>
		<div class="flex flex-wrap gap-2">
			{#each modes as modeOption}
				{@const ModeIcon = modeOption.icon}
				<button
					type="button"
					onclick={() => handleModeChange(modeOption.id)}
					disabled={isGenerating}
					class="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors {mode === modeOption.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-muted/80 border border-border'}"
					aria-pressed={mode === modeOption.id}
					title={modeOption.description}
				>
					<ModeIcon class="w-4 h-4" />
					{modeOption.label}
				</button>
			{/each}
		</div>
	</div>

	<!-- Prompt Input -->
	<div>
		<label for="model3d-prompt" class="block text-sm font-medium text-foreground mb-2">
			{#if mode === 'text-to-3d'}
				Describe your 3D model
			{:else if mode === 'image-to-3d'}
				Additional instructions (optional)
			{:else if mode === 'retexture'}
				Describe the new texture
			{:else if mode === 'rig'}
				Rigging instructions
			{:else}
				Animation prompt
			{/if}
		</label>
		<textarea
			id="model3d-prompt"
			bind:value={prompt}
			placeholder={mode === 'text-to-3d'
				? "A detailed medieval sword with ornate gold handle and dragon engravings..."
				: mode === 'image-to-3d'
				? "Optional: Add details about lighting, style, or modifications..."
				: "Describe your requirements..."}
			rows="4"
			class="w-full bg-muted border border-border rounded-xl px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
			disabled={isGenerating}
		></textarea>
	</div>

	<!-- Image Upload (for Image-to-3D mode) -->
	{#if mode === 'image-to-3d'}
		<div>
			<label class="block text-xs text-muted-foreground mb-2">
				Source Image
			</label>
			<p class="text-xs text-muted-foreground mb-3">
				Upload an image to convert into a 3D model.
			</p>

			{#if imagePreviewUrl}
				<div class="relative group w-full max-w-xs">
					<img
						src={imagePreviewUrl}
						alt="Source image preview"
						class="w-full aspect-square object-cover rounded-lg border border-border"
					/>
					<button
						type="button"
						onclick={clearImage}
						disabled={isGenerating}
						class="absolute -top-2 -right-2 w-6 h-6 bg-destructive text-destructive-foreground rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity disabled:opacity-50"
						aria-label="Remove image"
					>
						<X class="w-4 h-4" />
					</button>
				</div>
			{:else}
				<button
					type="button"
					onclick={triggerFileUpload}
					disabled={isGenerating}
					class="w-full max-w-xs aspect-square rounded-lg border-2 border-dashed border-border hover:border-primary/50 flex flex-col items-center justify-center cursor-pointer transition-colors text-muted-foreground hover:text-foreground disabled:opacity-50 disabled:cursor-not-allowed"
					aria-label="Upload source image"
				>
					<Upload class="w-8 h-8 mb-2" />
					<span class="text-sm">Upload Image</span>
					<span class="text-xs mt-1">PNG, JPG, or WebP</span>
				</button>
			{/if}

			<input
				bind:this={fileInputElement}
				type="file"
				accept="image/*"
				onchange={handleFileUpload}
				class="hidden"
				aria-hidden="true"
			/>
		</div>
	{/if}

	<!-- Model Selector -->
	<div>
		<label class="block text-xs text-muted-foreground mb-2">Model</label>
		<div class="space-y-2">
			{#each ALL_3D_MODELS as model}
				{@const badges = getModelBadges(model)}
				<button
					type="button"
					onclick={() => handleModelChange(model.id)}
					disabled={isGenerating}
					class="w-full flex flex-col gap-2 p-3 rounded-lg border transition-colors text-left {selectedModelId === model.id ? 'border-primary bg-primary/5' : 'border-border hover:border-muted-foreground/50 hover:bg-muted'}"
					aria-pressed={selectedModelId === model.id}
				>
					<div class="flex items-start gap-3">
						<div class="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center {selectedModelId === model.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
							<Box class="w-4 h-4" />
						</div>
						<div class="flex-1 min-w-0">
							<div class="text-sm font-medium text-foreground">{model.displayName}</div>
							<div class="text-xs text-muted-foreground">{model.description}</div>
						</div>
						{#if selectedModelId === model.id}
							<div class="shrink-0 w-2 h-2 rounded-full bg-primary mt-2"></div>
						{/if}
					</div>
					<!-- Capability Badges -->
					{#if badges.length > 0}
						<div class="flex flex-wrap gap-1 ml-11">
							{#each badges as badge}
								<span class="text-[10px] px-1.5 py-0.5 rounded {badge.color}">
									{badge.label}
								</span>
							{/each}
						</div>
					{/if}
				</button>
			{/each}
		</div>
	</div>

	<!-- Art Style -->
	<div>
		<label class="block text-xs text-muted-foreground mb-2">Art Style</label>
		<div class="flex gap-2">
			{#each artStyles as style}
				<button
					type="button"
					onclick={() => artStyle = style.value}
					disabled={isGenerating}
					class="flex-1 px-4 py-2.5 text-sm font-medium rounded-lg transition-colors {artStyle === style.value ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-muted/80 border border-border'}"
					aria-pressed={artStyle === style.value}
				>
					{style.label}
				</button>
			{/each}
		</div>
	</div>

	<!-- Advanced Options (Collapsible) -->
	<div class="border border-border rounded-lg overflow-hidden">
		<button
			type="button"
			onclick={() => showAdvanced = !showAdvanced}
			class="w-full flex items-center justify-between px-4 py-3 text-sm font-medium text-foreground bg-muted/50 hover:bg-muted transition-colors"
		>
			<span>Advanced Options</span>
			<ChevronDown class="w-4 h-4 transition-transform {showAdvanced ? 'rotate-180' : ''}" />
		</button>

		{#if showAdvanced}
			<div class="p-4 space-y-4 border-t border-border">
				<!-- Topology -->
				<div>
					<label class="block text-xs text-muted-foreground mb-2">Topology</label>
					<div class="flex gap-2">
						{#each topologies as topo}
							<button
								type="button"
								onclick={() => topology = topo.value}
								disabled={isGenerating}
								class="flex-1 px-4 py-2 text-sm font-medium rounded-lg transition-colors {topology === topo.value ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-muted/80 border border-border'}"
								aria-pressed={topology === topo.value}
							>
								{topo.label}
							</button>
						{/each}
					</div>
				</div>

				<!-- Target Polycount -->
				<div>
					<label class="block text-xs text-muted-foreground mb-2">
						Target Polycount: {targetPolycount.toLocaleString()}
					</label>
					<div class="flex flex-wrap gap-2">
						{#each polycountPresets as preset}
							<button
								type="button"
								onclick={() => targetPolycount = preset.value}
								disabled={isGenerating}
								class="px-3 py-1.5 text-xs font-medium rounded-lg transition-colors {targetPolycount === preset.value ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-muted/80 border border-border'}"
								aria-pressed={targetPolycount === preset.value}
							>
								{preset.label}
							</button>
						{/each}
					</div>
					<input
						type="range"
						min="5000"
						max="300000"
						step="5000"
						bind:value={targetPolycount}
						disabled={isGenerating}
						class="w-full mt-2 accent-primary"
					/>
				</div>

				<!-- PBR Toggle -->
				{#if currentModel?.capabilities.pbr}
					<div>
						<button
							type="button"
							onclick={() => enablePbr = !enablePbr}
							disabled={isGenerating}
							class="flex items-center gap-3 px-4 py-3 rounded-lg border transition-colors w-full {enablePbr ? 'border-primary bg-primary/5' : 'border-border hover:bg-muted'}"
							aria-pressed={enablePbr}
						>
							<div class="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center {enablePbr ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
								<Palette class="w-4 h-4" />
							</div>
							<div class="flex-1 text-left">
								<div class="text-sm font-medium text-foreground">
									{enablePbr ? 'PBR Textures Enabled' : 'PBR Textures Disabled'}
								</div>
								<div class="text-xs text-muted-foreground">
									Generate physically-based rendering textures
								</div>
							</div>
							<div class="shrink-0 w-10 h-6 rounded-full transition-colors {enablePbr ? 'bg-primary' : 'bg-muted-foreground/30'}">
								<div class="w-5 h-5 mt-0.5 rounded-full bg-white shadow-sm transition-transform {enablePbr ? 'translate-x-4' : 'translate-x-0.5'}"></div>
							</div>
						</button>
					</div>
				{/if}

				<!-- Output Formats Info -->
				<div class="p-3 rounded-lg bg-muted/50">
					<div class="text-xs font-medium text-muted-foreground mb-1">Available Output Formats</div>
					<div class="flex flex-wrap gap-1">
						{#each currentModel?.outputFormats || [] as format}
							<span class="text-xs px-2 py-0.5 rounded bg-muted text-foreground uppercase">
								{format}
							</span>
						{/each}
					</div>
				</div>
			</div>
		{/if}
	</div>

	<!-- Mode not supported warning -->
	{#if !modeSupported && currentModel}
		<div class="p-3 rounded-lg bg-destructive/10 border border-destructive/20">
			<div class="text-sm text-destructive">
				{currentModel.displayName} does not support {modes.find(m => m.id === mode)?.label}.
				Please select a different model or mode.
			</div>
		</div>
	{/if}

	<!-- Progress indicator -->
	{#if isGenerating && progress > 0}
		<div class="space-y-2">
			<div class="flex justify-between text-xs text-muted-foreground">
				<span>Generating 3D model...</span>
				<span>{progress}%</span>
			</div>
			<div class="w-full h-2 bg-muted rounded-full overflow-hidden">
				<div
					class="h-full bg-primary transition-all duration-300"
					style="width: {progress}%"
				></div>
			</div>
		</div>
	{/if}

	<!-- Generate Button -->
	<div class="pt-4 border-t border-border">
		<button
			type="button"
			onclick={handleGenerate}
			disabled={!canGenerate || !modeSupported}
			class="w-full px-6 py-3 text-sm font-medium bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
		>
			{#if isGenerating}
				<div class="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin"></div>
				Generating...
			{:else}
				<Sparkles class="w-4 h-4" />
				Generate 3D Model
			{/if}
		</button>
	</div>
</div>
