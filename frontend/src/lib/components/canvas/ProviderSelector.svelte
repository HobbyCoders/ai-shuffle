<script lang="ts">
	import type { ImageProvider, VideoProvider, ProviderModel } from '$lib/api/canvas';

	export let type: 'image' | 'video' = 'image';
	export let value: string;
	export let modelValue: string | null = null;
	export let onChange: (provider: string) => void;
	export let onModelChange: ((model: string | null) => void) | null = null;
	export let providers: (ImageProvider | VideoProvider)[] = [];
	export let loading: boolean = false;

	$: selectedProvider = providers.find((p) => p.id === value) || providers[0];
	$: availableModels = selectedProvider?.models || [];
	$: selectedModel = availableModels.find((m) => m.id === modelValue) || availableModels.find((m) => m.default) || availableModels[0];
	$: defaultModel = availableModels.find((m) => m.default);

	let isOpen = false;
	let isModelOpen = false;

	function selectProvider(provider: ImageProvider | VideoProvider) {
		onChange(provider.id);
		isOpen = false;
		// Reset model to default when provider changes
		if (onModelChange && provider.models.length > 0) {
			const defaultMod = provider.models.find((m) => m.default) || provider.models[0];
			onModelChange(defaultMod?.id || null);
		}
	}

	function selectModel(model: ProviderModel) {
		if (onModelChange) {
			onModelChange(model.id);
		}
		isModelOpen = false;
	}

	function handleClickOutside(event: MouseEvent) {
		const target = event.target as HTMLElement;
		if (!target.closest('.provider-selector')) {
			isOpen = false;
		}
		if (!target.closest('.model-selector')) {
			isModelOpen = false;
		}
	}
</script>

<svelte:window on:click={handleClickOutside} />

<div class="flex gap-2">
	<!-- Provider Selector -->
	<div class="provider-selector relative flex-1">
		<label class="block text-xs text-muted-foreground mb-1.5">Provider</label>
		{#if loading}
			<div class="w-full bg-muted border border-border rounded-lg px-3 py-2.5 text-sm">
				<div class="flex items-center gap-2">
					<div class="w-4 h-4 border-2 border-muted-foreground border-t-transparent rounded-full animate-spin"></div>
					<span class="text-muted-foreground">Loading...</span>
				</div>
			</div>
		{:else if providers.length === 0}
			<div class="w-full bg-muted border border-border rounded-lg px-3 py-2.5 text-sm text-muted-foreground">
				No providers available
			</div>
		{:else}
			<button
				type="button"
				onclick={() => (isOpen = !isOpen)}
				class="w-full bg-muted border border-border rounded-lg px-3 py-2.5 text-left text-sm text-foreground hover:bg-hover-overlay transition-colors flex items-center justify-between gap-2"
			>
				<div class="flex-1 min-w-0">
					<div class="font-medium truncate">{selectedProvider?.name || 'Select provider'}</div>
					<div class="text-xs text-muted-foreground truncate">{selectedProvider?.description || ''}</div>
				</div>
				<svg
					class="w-4 h-4 text-muted-foreground shrink-0 transition-transform {isOpen ? 'rotate-180' : ''}"
					fill="none"
					stroke="currentColor"
					viewBox="0 0 24 24"
				>
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
				</svg>
			</button>

			{#if isOpen}
				<div class="absolute top-full left-0 right-0 mt-1 bg-card border border-border rounded-lg shadow-lg z-20 overflow-hidden">
					{#each providers as provider}
						<button
							type="button"
							onclick={() => selectProvider(provider)}
							class="w-full px-3 py-2.5 text-left hover:bg-hover-overlay transition-colors flex items-center justify-between gap-2 {provider.id === value ? 'bg-primary/10' : ''}"
						>
							<div class="flex-1 min-w-0">
								<div class="text-sm font-medium text-foreground truncate">{provider.name}</div>
								<div class="text-xs text-muted-foreground truncate">{provider.description}</div>
							</div>
							{#if provider.id === value}
								<svg class="w-4 h-4 text-primary shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
								</svg>
							{/if}
						</button>
					{/each}
				</div>
			{/if}
		{/if}
	</div>

	<!-- Model Selector (if multiple models available and onModelChange provided) -->
	{#if onModelChange && availableModels.length > 1}
		<div class="model-selector relative flex-1">
			<label class="block text-xs text-muted-foreground mb-1.5">Model</label>
			<button
				type="button"
				onclick={() => (isModelOpen = !isModelOpen)}
				class="w-full bg-muted border border-border rounded-lg px-3 py-2.5 text-left text-sm text-foreground hover:bg-hover-overlay transition-colors flex items-center justify-between gap-2"
			>
				<div class="flex-1 min-w-0 flex items-center gap-2">
					<span class="font-medium truncate">{selectedModel?.name || 'Select model'}</span>
					{#if selectedModel?.has_audio}
						<span class="px-1.5 py-0.5 text-[10px] font-medium bg-primary/20 text-primary rounded">Audio</span>
					{/if}
					{#if selectedModel && defaultModel && selectedModel.id === defaultModel.id}
						<span class="px-1.5 py-0.5 text-[10px] font-medium bg-muted-foreground/20 text-muted-foreground rounded">Default</span>
					{/if}
				</div>
				<svg
					class="w-4 h-4 text-muted-foreground shrink-0 transition-transform {isModelOpen ? 'rotate-180' : ''}"
					fill="none"
					stroke="currentColor"
					viewBox="0 0 24 24"
				>
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
				</svg>
			</button>

			{#if isModelOpen}
				<div class="absolute top-full left-0 right-0 mt-1 bg-card border border-border rounded-lg shadow-lg z-20 overflow-hidden">
					{#each availableModels as model}
						<button
							type="button"
							onclick={() => selectModel(model)}
							class="w-full px-3 py-2.5 text-left hover:bg-hover-overlay transition-colors flex items-center justify-between gap-2 {model.id === modelValue ? 'bg-primary/10' : ''}"
						>
							<div class="flex-1 min-w-0 flex items-center gap-2">
								<span class="text-sm font-medium text-foreground truncate">{model.name}</span>
								{#if model.has_audio}
									<span class="px-1.5 py-0.5 text-[10px] font-medium bg-primary/20 text-primary rounded">Audio</span>
								{/if}
								{#if defaultModel && model.id === defaultModel.id}
									<span class="px-1.5 py-0.5 text-[10px] font-medium bg-muted-foreground/20 text-muted-foreground rounded">Default</span>
								{/if}
							</div>
							{#if model.id === modelValue}
								<svg class="w-4 h-4 text-primary shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
								</svg>
							{/if}
						</button>
					{/each}
				</div>
			{/if}
		</div>
	{/if}
</div>
