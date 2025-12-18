<script lang="ts">
	import {
		Image as ImageIcon,
		Film,
		RefreshCw,
		Trash2,
		Clock,
		CheckCircle2,
		XCircle,
		Loader2
	} from 'lucide-svelte';
	import type { DeckGeneration } from '../types';

	// Props
	interface Props {
		onSelect?: (generation: DeckGeneration) => void;
		onRegenerate?: (generation: DeckGeneration) => void;
		onDelete?: (generation: DeckGeneration) => void;
	}

	let { onSelect, onRegenerate, onDelete }: Props = $props();

	// Mock data for recent generations
	const mockHistory: DeckGeneration[] = [
		{
			id: 'h1',
			type: 'image',
			prompt: 'A serene mountain landscape at sunset with golden clouds reflecting on a calm lake',
			status: 'complete',
			thumbnailUrl: 'https://picsum.photos/seed/h1/200/150',
			resultUrl: 'https://picsum.photos/seed/h1/1200/800'
		},
		{
			id: 'h2',
			type: 'video',
			prompt: 'A cinematic drone shot flying over a misty forest at dawn',
			status: 'generating',
			progress: 65,
			thumbnailUrl: 'https://picsum.photos/seed/h2/200/150'
		},
		{
			id: 'h3',
			type: 'image',
			prompt: 'Futuristic cyberpunk city with neon lights and flying cars',
			status: 'complete',
			thumbnailUrl: 'https://picsum.photos/seed/h3/200/150',
			resultUrl: 'https://picsum.photos/seed/h3/1200/800'
		},
		{
			id: 'h4',
			type: 'image',
			prompt: 'Abstract digital art with flowing colors and geometric shapes',
			status: 'error'
		},
		{
			id: 'h5',
			type: 'video',
			prompt: 'Ocean waves crashing on a beach during golden hour',
			status: 'complete',
			thumbnailUrl: 'https://picsum.photos/seed/h5/200/150',
			resultUrl: 'https://picsum.photos/seed/h5/1200/800'
		},
		{
			id: 'h6',
			type: 'image',
			prompt: 'Portrait of a person in renaissance painting style',
			status: 'pending'
		}
	];

	// State
	let hoveredId: string | null = $state(null);

	// Handlers
	function handleSelect(generation: DeckGeneration) {
		if (generation.status === 'complete') {
			onSelect?.(generation);
		}
	}

	function handleRegenerate(generation: DeckGeneration, event: MouseEvent) {
		event.stopPropagation();
		onRegenerate?.(generation);
	}

	function handleDelete(generation: DeckGeneration, event: MouseEvent) {
		event.stopPropagation();
		onDelete?.(generation);
	}

	function getStatusIcon(status: DeckGeneration['status']) {
		switch (status) {
			case 'pending':
				return Clock;
			case 'generating':
				return Loader2;
			case 'complete':
				return CheckCircle2;
			case 'error':
				return XCircle;
			default:
				return Clock;
		}
	}

	function getStatusColor(status: DeckGeneration['status']): string {
		switch (status) {
			case 'pending':
				return 'text-muted-foreground';
			case 'generating':
				return 'text-primary';
			case 'complete':
				return 'text-green-500';
			case 'error':
				return 'text-destructive';
			default:
				return 'text-muted-foreground';
		}
	}

	function truncatePrompt(prompt: string, maxLength: number = 40): string {
		if (prompt.length <= maxLength) return prompt;
		return prompt.substring(0, maxLength) + '...';
	}
</script>

<div class="px-4 py-3">
	<div class="flex items-center justify-between mb-3">
		<h3 class="text-xs font-medium text-muted-foreground uppercase tracking-wide">
			Recent Generations
		</h3>
	</div>

	{#if mockHistory.length === 0}
		<!-- Empty state -->
		<div class="flex items-center justify-center py-8 text-muted-foreground">
			<div class="text-center">
				<Clock class="w-6 h-6 mx-auto mb-2" />
				<p class="text-sm">No recent generations</p>
			</div>
		</div>
	{:else}
		<!-- Horizontal scroll container -->
		<div class="flex gap-3 overflow-x-auto pb-2 -mx-4 px-4 scrollbar-thin scrollbar-thumb-border scrollbar-track-transparent">
			{#each mockHistory as generation (generation.id)}
				{@const StatusIcon = getStatusIcon(generation.status)}
				<div
					role="button"
					tabindex={generation.status === 'complete' ? 0 : -1}
					onclick={() => handleSelect(generation)}
					onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && handleSelect(generation)}
					onmouseenter={() => hoveredId = generation.id}
					onmouseleave={() => hoveredId = null}
					class="relative shrink-0 w-32 group {generation.status !== 'complete' ? 'pointer-events-none' : 'cursor-pointer'}"
					aria-label="View generation: {truncatePrompt(generation.prompt)}"
					aria-disabled={generation.status !== 'complete'}
				>
					<!-- Thumbnail card -->
					<div class="relative aspect-video rounded-lg overflow-hidden border border-border {generation.status === 'complete' ? 'hover:border-primary/50 cursor-pointer' : 'opacity-70 cursor-default'} transition-colors">
						{#if generation.thumbnailUrl}
							<img
								src={generation.thumbnailUrl}
								alt={generation.prompt}
								class="w-full h-full object-cover"
							/>
						{:else}
							<!-- Placeholder for items without thumbnail -->
							<div class="w-full h-full bg-muted flex items-center justify-center">
								{#if generation.type === 'image'}
									<ImageIcon class="w-6 h-6 text-muted-foreground" />
								{:else}
									<Film class="w-6 h-6 text-muted-foreground" />
								{/if}
							</div>
						{/if}

						<!-- Type badge -->
						<div class="absolute top-1.5 left-1.5 flex items-center gap-1 px-1.5 py-0.5 rounded bg-black/60 text-white text-[10px]">
							{#if generation.type === 'image'}
								<ImageIcon class="w-2.5 h-2.5" />
							{:else}
								<Film class="w-2.5 h-2.5" />
							{/if}
						</div>

						<!-- Status indicator -->
						<div class="absolute top-1.5 right-1.5 {getStatusColor(generation.status)}">
							{#if generation.status === 'generating'}
								<div class="relative">
									<StatusIcon class="w-4 h-4 animate-spin" />
									<!-- Progress ring overlay -->
									{#if generation.progress !== undefined}
										<div class="absolute -inset-0.5 flex items-center justify-center">
											<svg class="w-5 h-5 transform -rotate-90" viewBox="0 0 20 20">
												<circle
													cx="10"
													cy="10"
													r="8"
													fill="none"
													stroke="currentColor"
													stroke-width="2"
													stroke-dasharray={50.27}
													stroke-dashoffset={50.27 - (50.27 * generation.progress) / 100}
													class="text-primary"
												/>
											</svg>
										</div>
									{/if}
								</div>
							{:else}
								<StatusIcon class="w-4 h-4" />
							{/if}
						</div>

						<!-- Progress overlay for generating items -->
						{#if generation.status === 'generating' && generation.progress !== undefined}
							<div class="absolute bottom-0 left-0 right-0 h-1 bg-black/30">
								<div
									class="h-full bg-primary transition-all duration-300"
									style="width: {generation.progress}%"
								></div>
							</div>
						{/if}

						<!-- Hover actions (only for completed items) -->
						{#if generation.status === 'complete' && hoveredId === generation.id}
							<div class="absolute inset-0 bg-black/50 flex items-center justify-center gap-2 animate-fade-in pointer-events-auto">
								<div
									role="button"
									tabindex="0"
									onclick={(e) => handleRegenerate(generation, e)}
									onkeydown={(e) => e.key === 'Enter' && handleRegenerate(generation, e as unknown as MouseEvent)}
									class="p-1.5 rounded-full bg-white/20 text-white hover:bg-white/30 transition-colors cursor-pointer"
									aria-label="Re-generate"
									title="Re-generate with same prompt"
								>
									<RefreshCw class="w-3.5 h-3.5" />
								</div>
								<div
									role="button"
									tabindex="0"
									onclick={(e) => handleDelete(generation, e)}
									onkeydown={(e) => e.key === 'Enter' && handleDelete(generation, e as unknown as MouseEvent)}
									class="p-1.5 rounded-full bg-white/20 text-white hover:bg-destructive/80 transition-colors cursor-pointer"
									aria-label="Delete"
									title="Delete"
								>
									<Trash2 class="w-3.5 h-3.5" />
								</div>
							</div>
						{/if}
					</div>

					<!-- Prompt tooltip on hover -->
					{#if hoveredId === generation.id}
						<div class="absolute bottom-full left-0 right-0 mb-2 z-10 pointer-events-none">
							<div class="bg-popover border border-border rounded-lg shadow-lg p-2 text-xs text-foreground animate-fade-in">
								{generation.prompt}
							</div>
						</div>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</div>

<style>
	@keyframes fade-in {
		from {
			opacity: 0;
		}
		to {
			opacity: 1;
		}
	}

	.animate-fade-in {
		animation: fade-in 150ms ease-out;
	}

	/* Custom scrollbar styling */
	.scrollbar-thin {
		scrollbar-width: thin;
	}

	.scrollbar-thumb-border::-webkit-scrollbar {
		height: 6px;
	}

	.scrollbar-thumb-border::-webkit-scrollbar-track {
		background: transparent;
	}

	.scrollbar-thumb-border::-webkit-scrollbar-thumb {
		background-color: hsl(var(--border));
		border-radius: 3px;
	}

	.scrollbar-thumb-border::-webkit-scrollbar-thumb:hover {
		background-color: hsl(var(--muted-foreground));
	}
</style>
