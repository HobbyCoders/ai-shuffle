<script lang="ts">
	import {
		Box,
		Image,
		Palette,
		Bone,
		Play,
		Upload,
		X,
		ChevronDown,
		Sparkles,
		RotateCcw,
		Grid3x3,
		Maximize2,
		Eye,
		MessageSquare,
		Lightbulb,
		Download,
		Loader2
	} from 'lucide-svelte';
	import { studio } from '$lib/stores/studio';
	import {
		ALL_3D_MODELS,
		get3DModel,
		PROVIDER_DISPLAY_NAMES,
		type Model3DModel,
		type Model3DArtStyle,
		type Model3DTopology
	} from '$lib/types/ai-models';
	import { onMount } from 'svelte';

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
	let showAiAssistant = $state(false);

	// 3D Viewer state
	let canvasElement: HTMLCanvasElement | null = $state(null);
	let showWireframe = $state(false);
	let showGrid = $state(true);
	let currentModelUrl: string | null = $state(null);
	let isLoadingModel = $state(false);

	// Generation state
	let isGenerating = $state(false);
	let progress = $state(0);
	let taskId = $state<string | null>(null);

	// File input ref
	let fileInputElement: HTMLInputElement | null = $state(null);

	// AI suggestions
	let aiSuggestions = $state<string[]>([
		'Add more detail to the surface',
		'Optimize for game engines',
		'Increase polygon count'
	]);

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
	}

	function handleModeChange(newMode: Model3DMode) {
		mode = newMode;
		if (newMode !== 'image-to-3d' && imageFile) {
			clearImage();
		}
	}

	function handleFileUpload(event: Event) {
		const input = event.target as HTMLInputElement;
		if (!input.files?.length) return;

		const file = input.files[0];
		if (!file.type.startsWith('image/')) return;

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

			await onStartGeneration?.('3d', config);

			// Simulate progress for demo (in real implementation, this would be driven by API)
			const progressInterval = setInterval(() => {
				progress = Math.min(progress + 5, 95);
				if (progress >= 95) {
					clearInterval(progressInterval);
				}
			}, 500);

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

	function resetViewerCamera() {
		// Reset 3D camera to default position
		// Implementation would reset camera to initial view
	}

	function toggleWireframe() {
		showWireframe = !showWireframe;
	}

	function toggleGrid() {
		showGrid = !showGrid;
	}

	function toggleFullscreen() {
		const viewerElement = document.querySelector('.viewer-container');
		if (viewerElement) {
			if (!document.fullscreenElement) {
				viewerElement.requestFullscreen();
			} else {
				document.exitFullscreen();
			}
		}
	}

	function handleSuggestionClick(suggestion: string) {
		prompt = suggestion;
		showAiAssistant = false;
	}

	// Initialize 3D viewer
	onMount(() => {
		// Three.js initialization would go here
		// For now, this is a placeholder for the actual implementation

		return () => {
			// Cleanup Three.js resources
			if (imagePreviewUrl) {
				URL.revokeObjectURL(imagePreviewUrl);
			}
		};
	});
</script>

<div class="h-full flex flex-col bg-[var(--background)]">
	<!-- 3D Viewer Section - Hero Element (70% height) -->
	<div class="relative flex-[7] min-h-0">
		<!-- Viewer Container -->
		<div class="viewer-container absolute inset-0 overflow-hidden rounded-t-2xl">
			<!-- 3D Canvas -->
			<canvas
				bind:this={canvasElement}
				class="absolute inset-0 w-full h-full"
				style="background: linear-gradient(135deg, oklch(0.12 0.015 260) 0%, oklch(0.14 0.01 260) 50%, oklch(0.10 0.02 260) 100%);"
			></canvas>

			<!-- Loading State / Empty State -->
			{#if !currentModelUrl && !isLoadingModel}
				<div class="absolute inset-0 flex items-center justify-center">
					<div class="text-center space-y-4">
						<div class="relative w-32 h-32 mx-auto">
							<!-- Animated orbit rings -->
							<div class="absolute inset-0 border-2 border-primary/20 rounded-full animate-[spin_8s_linear_infinite]"></div>
							<div class="absolute inset-2 border-2 border-primary/30 rounded-full animate-[spin_6s_linear_infinite_reverse]"></div>
							<div class="absolute inset-4 border-2 border-primary/40 rounded-full animate-[spin_4s_linear_infinite]"></div>
							<div class="absolute inset-0 flex items-center justify-center">
								<Box class="w-12 h-12 text-primary/60" />
							</div>
						</div>
						<div class="text-muted-foreground">
							<p class="text-sm font-medium">No model loaded</p>
							<p class="text-xs mt-1">Generate a 3D model to begin</p>
						</div>
					</div>
				</div>
			{/if}

			<!-- Loading State -->
			{#if isLoadingModel}
				<div class="absolute inset-0 flex items-center justify-center bg-black/40 backdrop-blur-sm">
					<div class="text-center space-y-4">
						<Loader2 class="w-12 h-12 text-primary animate-spin mx-auto" />
						<p class="text-sm text-white font-medium">Loading model...</p>
					</div>
				</div>
			{/if}

			<!-- Viewer Controls Overlay (Top Right) -->
			<div class="absolute top-4 right-4 flex gap-2">
				<div class="glass rounded-xl p-1 flex gap-1 shadow-lg">
					<button
						type="button"
						onclick={resetViewerCamera}
						class="p-2 rounded-lg hover:bg-white/10 transition-colors text-white/80 hover:text-white"
						title="Reset view"
					>
						<RotateCcw class="w-4 h-4" />
					</button>
					<button
						type="button"
						onclick={toggleWireframe}
						class="p-2 rounded-lg hover:bg-white/10 transition-colors {showWireframe ? 'bg-white/10 text-primary' : 'text-white/80 hover:text-white'}"
						title="Toggle wireframe"
					>
						<Grid3x3 class="w-4 h-4" />
					</button>
					<button
						type="button"
						onclick={toggleGrid}
						class="p-2 rounded-lg hover:bg-white/10 transition-colors {showGrid ? 'bg-white/10 text-primary' : 'text-white/80 hover:text-white'}"
						title="Toggle grid"
					>
						<Eye class="w-4 h-4" />
					</button>
					<button
						type="button"
						onclick={toggleFullscreen}
						class="p-2 rounded-lg hover:bg-white/10 transition-colors text-white/80 hover:text-white"
						title="Fullscreen"
					>
						<Maximize2 class="w-4 h-4" />
					</button>
				</div>
			</div>

			<!-- Progress Indicator (Orbital) -->
			{#if isGenerating && progress > 0}
				<div class="absolute inset-0 flex items-center justify-center pointer-events-none">
					<div class="relative w-40 h-40">
						<!-- Animated progress ring -->
						<svg class="w-full h-full -rotate-90" viewBox="0 0 100 100">
							<!-- Background circle -->
							<circle
								cx="50"
								cy="50"
								r="45"
								fill="none"
								stroke="currentColor"
								stroke-width="2"
								class="text-white/10"
							/>
							<!-- Progress circle -->
							<circle
								cx="50"
								cy="50"
								r="45"
								fill="none"
								stroke="currentColor"
								stroke-width="2"
								stroke-dasharray="282.7"
								stroke-dashoffset={282.7 - (282.7 * progress) / 100}
								class="text-primary transition-all duration-300"
								stroke-linecap="round"
							/>
						</svg>
						<div class="absolute inset-0 flex flex-col items-center justify-center">
							<Sparkles class="w-8 h-8 text-primary mb-2 animate-pulse" />
							<span class="text-white text-lg font-semibold">{progress}%</span>
							<span class="text-white/60 text-xs mt-1">Generating...</span>
						</div>
					</div>
				</div>
			{/if}

			<!-- Model Info (Bottom Left) -->
			{#if currentModelUrl}
				<div class="absolute bottom-4 left-4">
					<div class="glass rounded-xl px-4 py-2 shadow-lg">
						<div class="text-xs text-white/60">Polycount</div>
						<div class="text-sm text-white font-medium">{targetPolycount.toLocaleString()}</div>
					</div>
				</div>
			{/if}
		</div>
	</div>

	<!-- Controls Section (30% height, scrollable) -->
	<div class="flex-[3] min-h-0 overflow-y-auto bg-[var(--card)] border-t border-[var(--border)]">
		<div class="p-6 space-y-6">
			<!-- Mode Selector -->
			<div>
				<label class="block text-xs font-medium text-muted-foreground mb-3 uppercase tracking-wider">
					Generation Mode
				</label>
				<div class="flex flex-wrap gap-2">
					{#each modes as modeOption}
						{@const ModeIcon = modeOption.icon}
						<button
							type="button"
							onclick={() => handleModeChange(modeOption.id)}
							disabled={isGenerating}
							class="group relative flex items-center gap-2 px-4 py-3 rounded-xl text-sm font-medium transition-all overflow-hidden {mode === modeOption.id ? 'bg-primary text-primary-foreground shadow-lg' : 'bg-[var(--muted)] text-foreground hover:bg-[var(--accent)] border border-[var(--border)]'}"
							title={modeOption.description}
						>
							{#if mode === modeOption.id}
								<div class="absolute inset-0 bg-gradient-to-br from-primary/90 to-primary"></div>
							{/if}
							<ModeIcon class="w-4 h-4 relative z-10" />
							<span class="relative z-10">{modeOption.label}</span>
						</button>
					{/each}
				</div>
			</div>

			<!-- Prompt Input -->
			<div>
				<div class="flex items-center justify-between mb-3">
					<label for="model3d-prompt" class="block text-xs font-medium text-muted-foreground uppercase tracking-wider">
						{#if mode === 'text-to-3d'}
							Describe your 3D model
						{:else if mode === 'image-to-3d'}
							Additional instructions
						{:else if mode === 'retexture'}
							Texture description
						{:else if mode === 'rig'}
							Rigging requirements
						{:else}
							Animation prompt
						{/if}
					</label>
					<button
						type="button"
						onclick={() => showAiAssistant = !showAiAssistant}
						class="flex items-center gap-1 text-xs text-primary hover:text-primary/80 transition-colors"
					>
						<Lightbulb class="w-3 h-3" />
						<span>AI Assist</span>
					</button>
				</div>

				<!-- AI Assistant Panel -->
				{#if showAiAssistant}
					<div class="mb-3 p-4 rounded-xl bg-[var(--muted)] border border-[var(--border)] space-y-3">
						<div class="flex items-center gap-2 text-sm font-medium text-foreground">
							<MessageSquare class="w-4 h-4 text-primary" />
							<span>AI Suggestions</span>
						</div>
						<div class="flex flex-wrap gap-2">
							{#each aiSuggestions as suggestion}
								<button
									type="button"
									onclick={() => handleSuggestionClick(suggestion)}
									class="px-3 py-1.5 rounded-lg bg-[var(--card)] hover:bg-[var(--accent)] border border-[var(--border)] text-xs text-foreground transition-colors"
								>
									{suggestion}
								</button>
							{/each}
						</div>
					</div>
				{/if}

				<textarea
					id="model3d-prompt"
					bind:value={prompt}
					placeholder={mode === 'text-to-3d'
						? "A futuristic sci-fi weapon with glowing energy cores and intricate mechanical details..."
						: mode === 'image-to-3d'
						? "Optional: Enhance lighting, add depth details, or specify style preferences..."
						: "Describe your requirements in detail..."}
					rows="4"
					class="w-full bg-[var(--input)] border border-[var(--border)] rounded-xl px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary resize-none transition-all"
					disabled={isGenerating}
				></textarea>
			</div>

			<!-- Image Upload (for Image-to-3D mode) -->
			{#if mode === 'image-to-3d'}
				<div>
					<label class="block text-xs font-medium text-muted-foreground mb-3 uppercase tracking-wider">
						Source Image
					</label>

					{#if imagePreviewUrl}
						<div class="relative group w-full max-w-xs">
							<img
								src={imagePreviewUrl}
								alt="Source image preview"
								class="w-full aspect-square object-cover rounded-xl border border-[var(--border)] shadow-lg"
							/>
							<button
								type="button"
								onclick={clearImage}
								disabled={isGenerating}
								class="absolute -top-2 -right-2 w-8 h-8 bg-destructive text-destructive-foreground rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity shadow-lg disabled:opacity-50"
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
							class="w-full max-w-xs aspect-square rounded-xl border-2 border-dashed border-[var(--border)] hover:border-primary/50 flex flex-col items-center justify-center cursor-pointer transition-all text-muted-foreground hover:text-foreground disabled:opacity-50 disabled:cursor-not-allowed group"
							aria-label="Upload source image"
						>
							<div class="p-4 rounded-full bg-[var(--muted)] group-hover:bg-primary/10 transition-colors mb-3">
								<Upload class="w-8 h-8" />
							</div>
							<span class="text-sm font-medium">Upload Image</span>
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

			<!-- Art Style -->
			<div>
				<label class="block text-xs font-medium text-muted-foreground mb-3 uppercase tracking-wider">
					Art Style
				</label>
				<div class="flex gap-3">
					{#each artStyles as style}
						<button
							type="button"
							onclick={() => artStyle = style.value}
							disabled={isGenerating}
							class="flex-1 px-4 py-3 text-sm font-medium rounded-xl transition-all {artStyle === style.value ? 'bg-primary text-primary-foreground shadow-lg' : 'bg-[var(--muted)] text-foreground hover:bg-[var(--accent)] border border-[var(--border)]'}"
						>
							{style.label}
						</button>
					{/each}
				</div>
			</div>

			<!-- Advanced Options Accordion -->
			<div class="border border-[var(--border)] rounded-xl overflow-hidden">
				<button
					type="button"
					onclick={() => showAdvanced = !showAdvanced}
					class="w-full flex items-center justify-between px-4 py-3 text-sm font-medium text-foreground bg-[var(--muted)]/50 hover:bg-[var(--muted)] transition-colors"
				>
					<span class="uppercase tracking-wider text-xs">Advanced Options</span>
					<ChevronDown class="w-4 h-4 transition-transform {showAdvanced ? 'rotate-180' : ''}" />
				</button>

				{#if showAdvanced}
					<div class="p-4 space-y-4 border-t border-[var(--border)]">
						<!-- Model Selector -->
						<div>
							<label class="block text-xs font-medium text-muted-foreground mb-2 uppercase tracking-wider">
								Model
							</label>
							<div class="space-y-2">
								{#each ALL_3D_MODELS as model}
									{@const badges = getModelBadges(model)}
									<button
										type="button"
										onclick={() => handleModelChange(model.id)}
										disabled={isGenerating}
										class="w-full flex flex-col gap-2 p-3 rounded-lg border transition-all text-left {selectedModelId === model.id ? 'border-primary bg-primary/5 shadow-glow' : 'border-[var(--border)] hover:border-muted-foreground/50 hover:bg-[var(--muted)]'}"
									>
										<div class="flex items-start gap-3">
											<div class="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center {selectedModelId === model.id ? 'bg-primary text-primary-foreground' : 'bg-[var(--muted)] text-muted-foreground'}">
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

						<!-- Topology -->
						<div>
							<label class="block text-xs font-medium text-muted-foreground mb-2 uppercase tracking-wider">
								Topology
							</label>
							<div class="flex gap-2">
								{#each topologies as topo}
									<button
										type="button"
										onclick={() => topology = topo.value}
										disabled={isGenerating}
										class="flex-1 px-4 py-2 text-sm font-medium rounded-lg transition-all {topology === topo.value ? 'bg-primary text-primary-foreground' : 'bg-[var(--muted)] text-foreground hover:bg-[var(--accent)] border border-[var(--border)]'}"
									>
										{topo.label}
									</button>
								{/each}
							</div>
						</div>

						<!-- Target Polycount -->
						<div>
							<label class="block text-xs font-medium text-muted-foreground mb-2 uppercase tracking-wider">
								Target Polycount: <span class="text-primary">{targetPolycount.toLocaleString()}</span>
							</label>
							<div class="flex flex-wrap gap-2 mb-3">
								{#each polycountPresets as preset}
									<button
										type="button"
										onclick={() => targetPolycount = preset.value}
										disabled={isGenerating}
										class="px-3 py-1.5 text-xs font-medium rounded-lg transition-all {targetPolycount === preset.value ? 'bg-primary text-primary-foreground' : 'bg-[var(--muted)] text-foreground hover:bg-[var(--accent)] border border-[var(--border)]'}"
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
								class="w-full h-2 bg-[var(--muted)] rounded-full appearance-none cursor-pointer [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-4 [&::-webkit-slider-thumb]:h-4 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-primary [&::-webkit-slider-thumb]:cursor-pointer [&::-moz-range-thumb]:w-4 [&::-moz-range-thumb]:h-4 [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:bg-primary [&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:cursor-pointer"
							/>
						</div>

						<!-- PBR Toggle -->
						{#if currentModel?.capabilities.pbr}
							<div>
								<button
									type="button"
									onclick={() => enablePbr = !enablePbr}
									disabled={isGenerating}
									class="flex items-center gap-3 px-4 py-3 rounded-xl border transition-all w-full {enablePbr ? 'border-primary bg-primary/5' : 'border-[var(--border)] hover:bg-[var(--muted)]'}"
								>
									<div class="shrink-0 w-10 h-10 rounded-lg flex items-center justify-center {enablePbr ? 'bg-primary text-primary-foreground' : 'bg-[var(--muted)] text-muted-foreground'}">
										<Palette class="w-5 h-5" />
									</div>
									<div class="flex-1 text-left">
										<div class="text-sm font-medium text-foreground">
											{enablePbr ? 'PBR Textures Enabled' : 'PBR Textures Disabled'}
										</div>
										<div class="text-xs text-muted-foreground">
											Physically-based rendering materials
										</div>
									</div>
									<div class="shrink-0 w-12 h-6 rounded-full transition-colors {enablePbr ? 'bg-primary' : 'bg-muted-foreground/30'} relative">
										<div class="w-5 h-5 mt-0.5 rounded-full bg-white shadow-sm transition-transform absolute {enablePbr ? 'translate-x-6' : 'translate-x-0.5'}"></div>
									</div>
								</button>
							</div>
						{/if}

						<!-- Output Formats -->
						<div class="p-3 rounded-lg bg-[var(--muted)]/50 border border-[var(--border)]">
							<div class="text-xs font-medium text-muted-foreground mb-2 uppercase tracking-wider">
								Output Formats
							</div>
							<div class="flex flex-wrap gap-1">
								{#each currentModel?.outputFormats || [] as format}
									<span class="text-xs px-2 py-1 rounded bg-[var(--card)] text-foreground uppercase font-medium border border-[var(--border)]">
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
				<div class="p-4 rounded-xl bg-destructive/10 border border-destructive/20">
					<div class="text-sm text-destructive font-medium">
						{currentModel.displayName} does not support {modes.find(m => m.id === mode)?.label}.
						Please select a different model or mode.
					</div>
				</div>
			{/if}

			<!-- Generate Button -->
			<div class="pt-2">
				<button
					type="button"
					onclick={handleGenerate}
					disabled={!canGenerate || !modeSupported}
					class="w-full px-6 py-4 text-sm font-semibold rounded-xl transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2 bg-gradient-to-br from-primary to-primary/80 text-primary-foreground shadow-lg hover:shadow-xl hover:scale-[1.02] active:scale-[0.98] disabled:hover:scale-100"
				>
					{#if isGenerating}
						<Loader2 class="w-5 h-5 animate-spin" />
						<span>Forging Model...</span>
					{:else}
						<Sparkles class="w-5 h-5" />
						<span>Generate 3D Model</span>
					{/if}
				</button>
			</div>
		</div>
	</div>
</div>

<style>
	/* Custom scrollbar for controls section */
	.overflow-y-auto {
		scrollbar-width: thin;
		scrollbar-color: var(--scrollbar-thumb) transparent;
	}

	.overflow-y-auto::-webkit-scrollbar {
		width: 6px;
	}

	.overflow-y-auto::-webkit-scrollbar-track {
		background: transparent;
	}

	.overflow-y-auto::-webkit-scrollbar-thumb {
		background: var(--scrollbar-thumb);
		border-radius: 3px;
	}

	.overflow-y-auto::-webkit-scrollbar-thumb:hover {
		background: var(--scrollbar-thumb-hover);
	}

	/* Glass morphism effect */
	.glass {
		background: var(--glass-bg);
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
		border: 1px solid var(--glass-border);
	}

	/* Smooth transitions */
	button {
		transition-property: all;
		transition-timing-function: cubic-bezier(0.4, 0, 0.2, 1);
		transition-duration: 200ms;
	}

	/* Prevent text selection on buttons */
	button {
		-webkit-user-select: none;
		user-select: none;
	}

	/* Canvas cursor */
	canvas {
		cursor: grab;
	}

	canvas:active {
		cursor: grabbing;
	}
</style>
