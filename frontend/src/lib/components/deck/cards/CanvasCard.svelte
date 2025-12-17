<script lang="ts">
	/**
	 * CanvasCard - Content creation card for The Deck
	 *
	 * Extends BaseCard with:
	 * - Large preview area for image/video
	 * - Prompt input below preview
	 * - Provider selector dropdown
	 * - Generation settings (aspect ratio, style, duration)
	 * - Generate button with loading state
	 * - Variations grid below
	 */
	import BaseCard from './BaseCard.svelte';
	import type { DeckGeneration, CanvasVariation, CanvasSettings, CanvasProvider, CanvasContentType } from './types';

	interface Props {
		id: string;
		title: string;
		generation: DeckGeneration;
		variations?: CanvasVariation[];
		settings?: CanvasSettings;
		pinned?: boolean;
		minimized?: boolean;
		active?: boolean;
		onpin?: () => void;
		onminimize?: () => void;
		onclose?: () => void;
		onactivate?: () => void;
		ongenerate?: (prompt: string, settings: CanvasSettings) => void;
		onedit?: (variationId: string) => void;
		onselectvariation?: (variationId: string) => void;
		onsettingschange?: (settings: CanvasSettings) => void;
	}

	let {
		id,
		title,
		generation,
		variations = [],
		settings = {
			aspectRatio: '1:1',
			provider: 'imagen'
		},
		pinned = false,
		minimized = false,
		active = false,
		onpin,
		onminimize,
		onclose,
		onactivate,
		ongenerate,
		onedit,
		onselectvariation,
		onsettingschange
	}: Props = $props();

	// Local state
	let promptValue = $state(generation.prompt || '');
	let showSettings = $state(false);
	let localSettings = $state<CanvasSettings>({ ...settings });

	// Sync local settings with prop
	$effect(() => {
		localSettings = { ...settings };
	});

	// Provider options
	const providers: { value: CanvasProvider; label: string; types: CanvasContentType[] }[] = [
		{ value: 'imagen', label: 'Imagen 3', types: ['image'] },
		{ value: 'gemini', label: 'Gemini', types: ['image'] },
		{ value: 'sora', label: 'Sora', types: ['video'] },
		{ value: 'veo', label: 'Veo 2', types: ['video'] }
	];

	// Aspect ratio options
	const aspectRatios: { value: CanvasSettings['aspectRatio']; label: string }[] = [
		{ value: '1:1', label: 'Square (1:1)' },
		{ value: '16:9', label: 'Landscape (16:9)' },
		{ value: '9:16', label: 'Portrait (9:16)' },
		{ value: '4:3', label: 'Standard (4:3)' },
		{ value: '3:4', label: 'Portrait (3:4)' }
	];

	// Duration options for video
	const durations = [5, 10, 15, 30, 60];

	// Is current provider video-capable
	const isVideoProvider = $derived(
		providers.find(p => p.value === localSettings.provider)?.types.includes('video') || false
	);

	function handleGenerate() {
		if (promptValue.trim() && !generation.isGenerating) {
			ongenerate?.(promptValue.trim(), localSettings);
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
			e.preventDefault();
			handleGenerate();
		}
	}

	function updateSetting<K extends keyof CanvasSettings>(key: K, value: CanvasSettings[K]) {
		localSettings = { ...localSettings, [key]: value };
		onsettingschange?.(localSettings);
	}

	// Get provider display info
	function getProviderInfo(provider: CanvasProvider): { color: string; icon: string } {
		switch (provider) {
			case 'imagen':
				return { color: 'text-blue-400', icon: 'M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z' };
			case 'gemini':
				return { color: 'text-purple-400', icon: 'M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z' };
			case 'sora':
				return { color: 'text-red-400', icon: 'M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z' };
			case 'veo':
				return { color: 'text-green-400', icon: 'M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664zM21 12a9 9 0 11-18 0 9 9 0 0118 0z' };
			default:
				return { color: 'text-foreground', icon: 'M12 6v6m0 0v6m0-6h6m-6 0H6' };
		}
	}

	const providerInfo = $derived(getProviderInfo(localSettings.provider));
</script>

<BaseCard
	{id}
	{title}
	type="canvas"
	{pinned}
	{minimized}
	{active}
	{onpin}
	{onminimize}
	{onclose}
	{onactivate}
>
	{#snippet headerActions()}
		<!-- Provider badge -->
		<span class="flex items-center gap-1.5 text-xs px-2 py-1 rounded-full bg-muted {providerInfo.color}">
			<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={providerInfo.icon} />
			</svg>
			{providers.find(p => p.value === localSettings.provider)?.label}
		</span>
	{/snippet}

	<div class="flex flex-col h-full overflow-hidden">
		<!-- Preview area -->
		<div class="flex-shrink-0 relative bg-muted/30 border-b border-border/30">
			<div
				class="relative w-full flex items-center justify-center overflow-hidden"
				style="aspect-ratio: {localSettings.aspectRatio.replace(':', '/')}; max-height: 300px;"
			>
				{#if generation.isGenerating}
					<!-- Loading state -->
					<div class="absolute inset-0 flex flex-col items-center justify-center bg-background/80 backdrop-blur-sm">
						<div class="relative">
							<svg class="w-12 h-12 animate-spin text-primary" fill="none" viewBox="0 0 24 24">
								<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
								<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
							</svg>
						</div>
						<p class="mt-3 text-sm text-muted-foreground">Generating...</p>
						<p class="text-xs text-muted-foreground/60 mt-1">This may take a moment</p>
					</div>
				{:else if generation.currentUrl}
					<!-- Content preview -->
					{#if generation.contentType === 'video'}
						<video
							src={generation.currentUrl}
							class="w-full h-full object-contain"
							controls
							loop
							muted
						>
							<track kind="captions" />
						</video>
					{:else}
						<img
							src={generation.currentUrl}
							alt={generation.prompt || 'Generated image'}
							class="w-full h-full object-contain"
						/>
					{/if}
				{:else}
					<!-- Empty state -->
					<div class="flex flex-col items-center justify-center p-8 text-center">
						<div class="w-16 h-16 rounded-2xl bg-muted/50 flex items-center justify-center mb-4">
							<svg class="w-8 h-8 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d={providerInfo.icon} />
							</svg>
						</div>
						<p class="text-sm text-muted-foreground">Enter a prompt to generate</p>
						<p class="text-xs text-muted-foreground/60 mt-1">{isVideoProvider ? 'Video' : 'Image'} will appear here</p>
					</div>
				{/if}
			</div>
		</div>

		<!-- Prompt and settings -->
		<div class="flex-1 overflow-y-auto">
			<div class="p-4 space-y-4">
				<!-- Prompt input -->
				<div class="space-y-2">
					<label for="canvas-prompt-{id}" class="text-xs font-medium text-muted-foreground">
						Prompt
					</label>
					<textarea
						id="canvas-prompt-{id}"
						bind:value={promptValue}
						onkeydown={handleKeydown}
						placeholder="Describe what you want to create..."
						rows="3"
						class="w-full px-3 py-2 bg-muted/50 border border-border/50 rounded-xl text-sm text-foreground placeholder:text-muted-foreground resize-none focus:outline-none focus:ring-2 focus:ring-primary/50"
					></textarea>
				</div>

				<!-- Settings toggle -->
				<button
					type="button"
					class="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
					onclick={() => showSettings = !showSettings}
				>
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
					</svg>
					Settings
					<svg
						class="w-4 h-4 transition-transform {showSettings ? 'rotate-180' : ''}"
						fill="none"
						stroke="currentColor"
						viewBox="0 0 24 24"
					>
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</button>

				{#if showSettings}
					<div class="space-y-4 pl-6 border-l-2 border-border/50">
						<!-- Provider selector -->
						<div class="space-y-2">
							<label for="provider-{id}" class="text-xs font-medium text-muted-foreground">Provider</label>
							<select
								id="provider-{id}"
								value={localSettings.provider}
								onchange={(e) => updateSetting('provider', (e.target as HTMLSelectElement).value as CanvasProvider)}
								class="w-full px-3 py-2 bg-muted/50 border border-border/50 rounded-lg text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
							>
								{#each providers as provider}
									<option value={provider.value}>{provider.label} ({provider.types.join(', ')})</option>
								{/each}
							</select>
						</div>

						<!-- Aspect ratio -->
						<div class="space-y-2">
							<label for="aspect-{id}" class="text-xs font-medium text-muted-foreground">Aspect Ratio</label>
							<select
								id="aspect-{id}"
								value={localSettings.aspectRatio}
								onchange={(e) => updateSetting('aspectRatio', (e.target as HTMLSelectElement).value as CanvasSettings['aspectRatio'])}
								class="w-full px-3 py-2 bg-muted/50 border border-border/50 rounded-lg text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
							>
								{#each aspectRatios as ratio}
									<option value={ratio.value}>{ratio.label}</option>
								{/each}
							</select>
						</div>

						<!-- Style (optional) -->
						<div class="space-y-2">
							<label for="style-{id}" class="text-xs font-medium text-muted-foreground">Style (optional)</label>
							<input
								id="style-{id}"
								type="text"
								value={localSettings.style || ''}
								onchange={(e) => updateSetting('style', (e.target as HTMLInputElement).value || undefined)}
								placeholder="e.g., cinematic, photorealistic, anime"
								class="w-full px-3 py-2 bg-muted/50 border border-border/50 rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
							/>
						</div>

						<!-- Duration (for video) -->
						{#if isVideoProvider}
							<div class="space-y-2">
								<label for="duration-{id}" class="text-xs font-medium text-muted-foreground">Duration (seconds)</label>
								<select
									id="duration-{id}"
									value={localSettings.duration || 5}
									onchange={(e) => updateSetting('duration', parseInt((e.target as HTMLSelectElement).value))}
									class="w-full px-3 py-2 bg-muted/50 border border-border/50 rounded-lg text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
								>
									{#each durations as duration}
										<option value={duration}>{duration}s</option>
									{/each}
								</select>
							</div>
						{/if}
					</div>
				{/if}

				<!-- Variations grid -->
				{#if variations.length > 0}
					<div class="space-y-2">
						<h4 class="text-xs font-medium text-muted-foreground">Previous Generations</h4>
						<div class="grid grid-cols-4 gap-2">
							{#each variations.slice(0, 8) as variation (variation.id)}
								<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
								<div
									class="relative aspect-square rounded-lg overflow-hidden border border-border/50 hover:border-primary/50 transition-colors group cursor-pointer"
									onclick={() => onselectvariation?.(variation.id)}
									title={variation.prompt}
								>
									{#if variation.contentType === 'video'}
										<video
											src={variation.url}
											class="w-full h-full object-cover"
											muted
											loop
											playsinline
										>
											<track kind="captions" />
										</video>
										<div class="absolute inset-0 flex items-center justify-center bg-black/30">
											<svg class="w-4 h-4 text-white" fill="currentColor" viewBox="0 0 24 24">
												<path d="M5 3l14 9-14 9V3z" />
											</svg>
										</div>
									{:else}
										<img
											src={variation.url}
											alt={variation.prompt}
											class="w-full h-full object-cover"
										/>
									{/if}
									<!-- Edit overlay -->
									<div class="absolute inset-0 bg-black/50 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center">
										<button
											type="button"
											class="p-1.5 rounded-lg bg-white/20 text-white hover:bg-white/30 transition-colors"
											onclick={(e) => { e.stopPropagation(); onedit?.(variation.id); }}
											title="Edit"
										>
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
											</svg>
										</button>
									</div>
								</div>
							{/each}
						</div>
						{#if variations.length > 8}
							<p class="text-xs text-muted-foreground text-center">
								+{variations.length - 8} more
							</p>
						{/if}
					</div>
				{/if}
			</div>
		</div>
	</div>

	{#snippet footer()}
		<div class="flex items-center gap-2 px-4 py-3">
			<button
				type="button"
				class="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors font-medium"
				onclick={handleGenerate}
				disabled={!promptValue.trim() || generation.isGenerating}
			>
				{#if generation.isGenerating}
					<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
						<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
						<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
					</svg>
					Generating...
				{:else}
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
					</svg>
					Generate
				{/if}
			</button>
		</div>
	{/snippet}
</BaseCard>
