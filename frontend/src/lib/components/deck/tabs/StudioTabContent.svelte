<script lang="ts">
	/**
	 * StudioTabContent - Image/video generations
	 */

	import { Image, Video, Loader2 } from 'lucide-svelte';
	import type { DeckGeneration } from '../types';

	interface Props {
		generations?: DeckGeneration[];
		onGenerationClick?: (generation: DeckGeneration) => void;
	}

	let {
		generations = [],
		onGenerationClick
	}: Props = $props();

	// Separate by status
	const generating = $derived(generations.filter(g => g.status === 'generating' || g.status === 'pending'));
	const completed = $derived(generations.filter(g => g.status === 'complete'));
	const failed = $derived(generations.filter(g => g.status === 'error'));

	function getTypeIcon(type: DeckGeneration['type']) {
		switch (type) {
			case 'video':
				return Video;
			case 'image':
			default:
				return Image;
		}
	}
</script>

<div class="studio-content">
	<!-- In Progress -->
	{#if generating.length > 0}
		<div class="section">
			<div class="section-header">
				<Loader2 size={14} strokeWidth={1.5} class="spinning" />
				<span>Generating</span>
				<span class="section-count active">{generating.length}</span>
			</div>

			<div class="generations-grid">
				{#each generating as generation (generation.id)}
					<button
						class="generation-item generating"
						onclick={() => onGenerationClick?.(generation)}
						title={generation.prompt}
					>
						<div class="generation-placeholder">
							<svelte:component this={getTypeIcon(generation.type)} size={20} strokeWidth={1.5} />
						</div>
						{#if generation.progress !== undefined}
							<div class="generation-progress">
								<div class="progress-bar" style:width="{generation.progress}%"></div>
							</div>
						{/if}
						<div class="generation-type-badge">{generation.type}</div>
					</button>
				{/each}
			</div>
		</div>
	{/if}

	<!-- Completed -->
	<div class="section">
		<div class="section-header">
			<Image size={14} strokeWidth={1.5} />
			<span>Recent</span>
			{#if completed.length > 0}
				<span class="section-count">{completed.length}</span>
			{/if}
		</div>

		{#if completed.length === 0 && generating.length === 0}
			<div class="empty-state">No generations yet</div>
		{:else if completed.length > 0}
			<div class="generations-grid">
				{#each completed as generation (generation.id)}
					<button
						class="generation-item"
						onclick={() => onGenerationClick?.(generation)}
						title={generation.prompt}
					>
						{#if generation.thumbnailUrl}
							<img
								src={generation.thumbnailUrl}
								alt={generation.prompt}
								class="generation-thumbnail"
							/>
						{:else}
							<div class="generation-placeholder">
								<svelte:component this={getTypeIcon(generation.type)} size={20} strokeWidth={1.5} />
							</div>
						{/if}
						<div class="generation-type-badge">{generation.type}</div>
					</button>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Failed -->
	{#if failed.length > 0}
		<div class="section error">
			<div class="section-header">
				<span>Failed</span>
				<span class="section-count error">{failed.length}</span>
			</div>

			<div class="generations-grid">
				{#each failed as generation (generation.id)}
					<button
						class="generation-item error"
						onclick={() => onGenerationClick?.(generation)}
						title={generation.prompt}
					>
						<div class="generation-placeholder">
							<svelte:component this={getTypeIcon(generation.type)} size={20} strokeWidth={1.5} />
						</div>
					</button>
				{/each}
			</div>
		</div>
	{/if}
</div>

<style>
	.studio-content {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.section {
		background: var(--secondary);
		border: 1px solid var(--border);
		border-radius: 10px;
		overflow: hidden;
	}

	.section.error {
		border-color: color-mix(in oklch, var(--destructive) 30%, var(--border));
	}

	.section-header {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 12px;
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--foreground);
		border-bottom: 1px solid var(--border);
	}

	.section-header :global(.spinning) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	.section-count {
		padding: 2px 6px;
		background: var(--muted);
		border-radius: 10px;
		font-size: 0.625rem;
		font-weight: 700;
	}

	.section-count.active {
		background: color-mix(in oklch, var(--primary) 20%, transparent);
		color: var(--primary);
	}

	.section-count.error {
		background: color-mix(in oklch, var(--destructive) 20%, transparent);
		color: var(--destructive);
	}

	.empty-state {
		padding: 24px 16px;
		text-align: center;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.generations-grid {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 6px;
		padding: 8px;
	}

	.generation-item {
		aspect-ratio: 1;
		border-radius: 8px;
		overflow: hidden;
		background: var(--muted);
		border: 1px solid var(--border);
		cursor: pointer;
		transition: all 0.15s ease;
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.generation-item:hover {
		border-color: var(--primary);
		transform: scale(1.02);
	}

	.generation-item.generating {
		border-color: var(--primary);
		animation: pulse 2s ease-in-out infinite;
	}

	.generation-item.error {
		border-color: var(--destructive);
		opacity: 0.7;
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.6; }
	}

	.generation-thumbnail {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}

	.generation-placeholder {
		color: var(--muted-foreground);
	}

	.generation-progress {
		position: absolute;
		bottom: 0;
		left: 0;
		right: 0;
		height: 3px;
		background: var(--muted);
	}

	.progress-bar {
		height: 100%;
		background: linear-gradient(90deg, var(--primary), color-mix(in oklch, var(--primary) 70%, #ec4899));
		transition: width 0.3s ease;
	}

	.generation-type-badge {
		position: absolute;
		bottom: 4px;
		right: 4px;
		font-size: 0.5rem;
		font-weight: 600;
		text-transform: uppercase;
		color: white;
		background: rgba(0, 0, 0, 0.6);
		padding: 2px 4px;
		border-radius: 3px;
	}
</style>
