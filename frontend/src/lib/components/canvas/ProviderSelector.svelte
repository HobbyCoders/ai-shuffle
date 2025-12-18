<script lang="ts">
	export let type: 'image' | 'video' = 'image';
	export let value: string;
	export let onChange: (provider: string) => void;

	interface Provider {
		id: string;
		name: string;
		desc: string;
	}

	const imageProviders: Provider[] = [
		{ id: 'google-gemini', name: 'Nano Banana', desc: 'Fast, editing, references' },
		{ id: 'google-imagen', name: 'Imagen 4', desc: 'Highest quality' },
		{ id: 'openai-gpt-image', name: 'GPT Image', desc: 'Accurate text' }
	];

	const videoProviders: Provider[] = [
		{ id: 'google-veo', name: 'Veo', desc: 'Extension, bridging, audio' },
		{ id: 'openai-sora', name: 'Sora', desc: 'Fast, up to 12s' }
	];

	$: providers = type === 'image' ? imageProviders : videoProviders;
	$: selectedProvider = providers.find(p => p.id === value) || providers[0];

	let isOpen = false;

	function selectProvider(provider: Provider) {
		onChange(provider.id);
		isOpen = false;
	}

	function handleClickOutside(event: MouseEvent) {
		const target = event.target as HTMLElement;
		if (!target.closest('.provider-selector')) {
			isOpen = false;
		}
	}
</script>

<svelte:window on:click={handleClickOutside} />

<div class="provider-selector relative">
	<label class="block text-xs text-muted-foreground mb-1.5">Provider</label>
	<button
		type="button"
		onclick={() => isOpen = !isOpen}
		class="w-full bg-muted border border-border rounded-lg px-3 py-2.5 text-left text-sm text-foreground hover:bg-hover-overlay transition-colors flex items-center justify-between gap-2"
	>
		<div class="flex-1 min-w-0">
			<div class="font-medium truncate">{selectedProvider.name}</div>
			<div class="text-xs text-muted-foreground truncate">{selectedProvider.desc}</div>
		</div>
		<svg class="w-4 h-4 text-muted-foreground shrink-0 transition-transform {isOpen ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
						<div class="text-xs text-muted-foreground truncate">{provider.desc}</div>
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
</div>
