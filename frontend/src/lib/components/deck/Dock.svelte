<script lang="ts">
	/**
	 * Dock - Bottom bar for running processes and quick actions
	 *
	 * Features:
	 * - Left: Running agent avatars (circular with progress rings)
	 * - Center: Media generation thumbnails (square with spinners)
	 * - Right: Quick action buttons (+Chat, +Agent, +Create)
	 */

	import type { DeckAgent, DeckGeneration } from './types';

	interface Props {
		/** List of running agents */
		agents?: DeckAgent[];
		/** List of media generations */
		generations?: DeckGeneration[];
		/** Called when an agent is clicked */
		onAgentClick?: (agent: DeckAgent) => void;
		/** Called when a generation is clicked */
		onGenerationClick?: (generation: DeckGeneration) => void;
		/** Called when new chat button is clicked */
		onNewChat?: () => void;
		/** Called when new agent button is clicked */
		onNewAgent?: () => void;
		/** Called when new create button is clicked */
		onNewCreate?: () => void;
	}

	let {
		agents = [],
		generations = [],
		onAgentClick,
		onGenerationClick,
		onNewChat,
		onNewAgent,
		onNewCreate
	}: Props = $props();

	// Filter to show only active items
	let activeAgents = $derived(agents.filter((a) => a.status === 'running' || a.status === 'paused'));
	let activeGenerations = $derived(
		generations.filter((g) => g.status === 'generating' || g.status === 'pending')
	);

	function getProgressOffset(progress: number = 0): number {
		// For circular progress: circumference = 2 * PI * r
		// With r=18, circumference ≈ 113.1
		const circumference = 113.1;
		return circumference - (progress / 100) * circumference;
	}

	function getStatusColor(status: string): string {
		switch (status) {
			case 'running':
			case 'generating':
				return 'stroke-success';
			case 'paused':
			case 'pending':
				return 'stroke-warning';
			case 'completed':
				return 'stroke-primary';
			case 'failed':
				return 'stroke-destructive';
			default:
				return 'stroke-muted-foreground';
		}
	}

	function getAgentInitials(name: string): string {
		return name
			.split(' ')
			.map((w) => w[0])
			.join('')
			.substring(0, 2)
			.toUpperCase();
	}
</script>

<footer
	class="dock h-14 flex items-center justify-between px-4 bg-card/80 backdrop-blur-xl border-t border-border"
	aria-label="Dock"
>
	<!-- Left: Running Agents -->
	<div class="dock-section agents-section flex items-center gap-2" role="group" aria-label="Running agents">
		{#if activeAgents.length === 0}
			<span class="text-xs text-muted-foreground">No active agents</span>
		{:else}
			{#each activeAgents.slice(0, 5) as agent}
				<button
					onclick={() => onAgentClick?.(agent)}
					class="agent-avatar relative group"
					aria-label="{agent.name} - {agent.status}"
				>
					<!-- Progress ring -->
					<svg class="w-10 h-10 -rotate-90" viewBox="0 0 40 40" aria-hidden="true">
						<!-- Background circle -->
						<circle
							cx="20"
							cy="20"
							r="18"
							fill="none"
							stroke="currentColor"
							stroke-width="2"
							class="text-muted/30"
						/>
						<!-- Progress circle -->
						<circle
							cx="20"
							cy="20"
							r="18"
							fill="none"
							stroke-width="2"
							stroke-linecap="round"
							class="{getStatusColor(agent.status)} transition-all duration-500"
							stroke-dasharray="113.1"
							stroke-dashoffset={getProgressOffset(agent.progress || 0)}
						/>
					</svg>

					<!-- Avatar content -->
					<div
						class="
							absolute inset-0 m-1 rounded-full
							bg-secondary flex items-center justify-center
							text-xs font-bold text-foreground
							overflow-hidden
						"
					>
						{#if agent.avatarUrl}
							<img src={agent.avatarUrl} alt="" class="w-full h-full object-cover" />
						{:else}
							{getAgentInitials(agent.name)}
						{/if}
					</div>

					<!-- Pulse animation for running -->
					{#if agent.status === 'running'}
						<span
							class="absolute inset-0 rounded-full bg-success/20 animate-ping"
							aria-hidden="true"
						></span>
					{/if}

					<!-- Tooltip -->
					<span
						class="
							absolute bottom-full left-1/2 -translate-x-1/2 mb-2
							px-2 py-1 rounded-md
							bg-popover text-popover-foreground text-xs font-medium
							opacity-0 invisible group-hover:opacity-100 group-hover:visible
							transition-all duration-200 whitespace-nowrap z-50
							shadow-lg border border-border
						"
						role="tooltip"
					>
						{agent.name}
						{#if agent.currentTask}
							<span class="block text-[10px] text-muted-foreground">{agent.currentTask}</span>
						{/if}
					</span>
				</button>
			{/each}
			{#if activeAgents.length > 5}
				<span class="text-xs text-muted-foreground">+{activeAgents.length - 5} more</span>
			{/if}
		{/if}
	</div>

	<!-- Center: Generations -->
	<div
		class="dock-section generations-section flex items-center gap-2"
		role="group"
		aria-label="Media generations"
	>
		{#each activeGenerations.slice(0, 4) as generation}
			<button
				onclick={() => onGenerationClick?.(generation)}
				class="
					generation-thumbnail relative w-10 h-10 rounded-lg
					bg-muted overflow-hidden group
					border border-border hover:border-primary
					transition-all duration-200
				"
				aria-label="{generation.type} generation - {generation.status}"
			>
				{#if generation.thumbnailUrl}
					<img
						src={generation.thumbnailUrl}
						alt=""
						class="w-full h-full object-cover"
					/>
				{/if}

				<!-- Spinner overlay for generating -->
				{#if generation.status === 'generating' || generation.status === 'pending'}
					<div
						class="absolute inset-0 flex items-center justify-center bg-background/60"
						aria-hidden="true"
					>
						<svg class="w-5 h-5 text-primary animate-spin" fill="none" viewBox="0 0 24 24">
							<circle
								class="opacity-25"
								cx="12"
								cy="12"
								r="10"
								stroke="currentColor"
								stroke-width="4"
							></circle>
							<path
								class="opacity-75"
								fill="currentColor"
								d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
							></path>
						</svg>
					</div>

					<!-- Progress bar at bottom -->
					{#if generation.progress !== undefined}
						<div class="absolute bottom-0 left-0 right-0 h-1 bg-muted/50">
							<div
								class="h-full bg-primary transition-all duration-300"
								style="width: {generation.progress}%"
							></div>
						</div>
					{/if}
				{/if}

				<!-- Type indicator -->
				<div
					class="absolute top-0.5 right-0.5 w-4 h-4 rounded bg-background/80 flex items-center justify-center"
				>
					{#if generation.type === 'image'}
						<svg class="w-2.5 h-2.5 text-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="2"
								d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"
							/>
						</svg>
					{:else}
						<svg class="w-2.5 h-2.5 text-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="2"
								d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"
							/>
						</svg>
					{/if}
				</div>

				<!-- Tooltip -->
				<span
					class="
						absolute bottom-full left-1/2 -translate-x-1/2 mb-2
						px-2 py-1 rounded-md
						bg-popover text-popover-foreground text-xs font-medium
						opacity-0 invisible group-hover:opacity-100 group-hover:visible
						transition-all duration-200 whitespace-nowrap z-50
						shadow-lg border border-border max-w-[200px] truncate
					"
					role="tooltip"
				>
					{generation.prompt}
				</span>
			</button>
		{/each}
		{#if activeGenerations.length > 4}
			<span class="text-xs text-muted-foreground">+{activeGenerations.length - 4}</span>
		{/if}
	</div>

	<!-- Right: Quick Actions -->
	<div class="dock-section actions-section flex items-center gap-1" role="group" aria-label="Quick actions">
		<button
			onclick={() => onNewChat?.()}
			class="
				quick-action flex items-center gap-1.5 px-3 py-1.5 rounded-lg
				bg-secondary hover:bg-accent
				text-foreground text-sm font-medium
				transition-all duration-200
				border border-border hover:border-primary/50
			"
			aria-label="New chat"
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
			</svg>
			<span class="hidden sm:inline">Chat</span>
		</button>

		<button
			onclick={() => onNewAgent?.()}
			class="
				quick-action flex items-center gap-1.5 px-3 py-1.5 rounded-lg
				bg-secondary hover:bg-accent
				text-foreground text-sm font-medium
				transition-all duration-200
				border border-border hover:border-primary/50
			"
			aria-label="New agent"
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
				<path
					stroke-linecap="round"
					stroke-linejoin="round"
					stroke-width="2"
					d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"
				/>
			</svg>
			<span class="hidden sm:inline">Agent</span>
		</button>

		<button
			onclick={() => onNewCreate?.()}
			class="
				quick-action flex items-center gap-1.5 px-3 py-1.5 rounded-lg
				bg-primary hover:bg-primary/90
				text-primary-foreground text-sm font-medium
				transition-all duration-200
				shadow-glow-soft hover:shadow-glow
			"
			aria-label="New creation"
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
				<path
					stroke-linecap="round"
					stroke-linejoin="round"
					stroke-width="2"
					d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"
				/>
			</svg>
			<span class="hidden sm:inline">Create</span>
		</button>
	</div>
</footer>

<style>
	.dock {
		grid-area: dock;
	}

	.shadow-glow {
		box-shadow: 0 0 12px var(--glow-color), 0 0 4px var(--glow-color-soft);
	}

	.shadow-glow-soft {
		box-shadow: 0 0 8px var(--glow-color-soft);
	}

	.agent-avatar {
		position: relative;
	}

	/* Mobile adjustments */
	@media (max-width: 640px) {
		.dock {
			padding-left: 0.75rem;
			padding-right: 0.75rem;
		}

		.dock-section {
			gap: 0.25rem;
		}

		.agents-section,
		.generations-section {
			flex: 1;
			justify-content: flex-start;
		}

		.actions-section {
			flex-shrink: 0;
		}

		.quick-action {
			padding: 0.5rem;
		}

		/* Hide tooltips on mobile */
		.agent-avatar span[role='tooltip'],
		.generation-thumbnail span[role='tooltip'] {
			display: none;
		}
	}

	/* Ensure dock doesn't get too crowded */
	@media (max-width: 400px) {
		.generations-section {
			display: none;
		}
	}
</style>
