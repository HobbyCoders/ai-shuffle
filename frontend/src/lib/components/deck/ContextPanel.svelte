<script lang="ts">
	/**
	 * ContextPanel - Right sidebar showing contextual information
	 *
	 * Features:
	 * - Collapse/expand toggle button
	 * - Collapsible sections: Active Threads, Running Agents, Generations, Session Info
	 * - Glassmorphism styling
	 * - Responsive design
	 */

	import type { DeckSession, DeckAgent, DeckGeneration, SessionInfo } from './types';

	interface Props {
		/** Whether the panel is collapsed */
		collapsed?: boolean;
		/** List of chat sessions */
		sessions?: DeckSession[];
		/** List of running agents */
		agents?: DeckAgent[];
		/** List of media generations */
		generations?: DeckGeneration[];
		/** Current session info */
		currentSession?: SessionInfo | null;
		/** Called when collapse state changes */
		onCollapsedChange?: (collapsed: boolean) => void;
		/** Called when a session is clicked */
		onSessionClick?: (session: DeckSession) => void;
		/** Called when an agent is clicked */
		onAgentClick?: (agent: DeckAgent) => void;
		/** Called when a generation is clicked */
		onGenerationClick?: (generation: DeckGeneration) => void;
	}

	let {
		collapsed = false,
		sessions = [],
		agents = [],
		generations = [],
		currentSession = null,
		onCollapsedChange,
		onSessionClick,
		onAgentClick,
		onGenerationClick
	}: Props = $props();

	// Section collapse states
	let sectionsCollapsed = $state<Record<string, boolean>>({
		threads: false,
		agents: false,
		generations: false,
		session: false
	});

	function toggleSection(section: string) {
		sectionsCollapsed[section] = !sectionsCollapsed[section];
	}

	function toggleCollapsed() {
		onCollapsedChange?.(!collapsed);
	}

	function formatCost(cost: number): string {
		return cost < 0.01 ? '<$0.01' : `$${cost.toFixed(2)}`;
	}

	function formatTokens(count: number): string {
		if (count >= 1000000) return `${(count / 1000000).toFixed(1)}M`;
		if (count >= 1000) return `${(count / 1000).toFixed(1)}K`;
		return count.toString();
	}

	function formatTimeAgo(date: Date): string {
		const now = new Date();
		const diff = now.getTime() - new Date(date).getTime();
		const minutes = Math.floor(diff / 60000);
		const hours = Math.floor(minutes / 60);
		const days = Math.floor(hours / 24);

		if (days > 0) return `${days}d ago`;
		if (hours > 0) return `${hours}h ago`;
		if (minutes > 0) return `${minutes}m ago`;
		return 'Just now';
	}

	function getStatusColor(status: string): string {
		switch (status) {
			case 'running':
			case 'generating':
				return 'text-success';
			case 'paused':
			case 'pending':
				return 'text-warning';
			case 'completed':
				return 'text-primary';
			case 'failed':
				return 'text-destructive';
			default:
				return 'text-muted-foreground';
		}
	}

	function getStatusBgColor(status: string): string {
		switch (status) {
			case 'running':
			case 'generating':
				return 'bg-success';
			case 'paused':
			case 'pending':
				return 'bg-warning';
			case 'completed':
				return 'bg-primary';
			case 'failed':
				return 'bg-destructive';
			default:
				return 'bg-muted-foreground';
		}
	}

	// Active sessions count
	let activeSessionsCount = $derived(sessions.filter((s) => s.active).length);
	let runningAgentsCount = $derived(agents.filter((a) => a.status === 'running').length);
	let pendingGenerationsCount = $derived(
		generations.filter((g) => g.status === 'pending' || g.status === 'generating').length
	);
</script>

<aside
	class="context-panel h-full flex flex-col bg-card/50 backdrop-blur-xl border-l border-border transition-all duration-300"
	class:collapsed
	aria-label="Context panel"
>
	<!-- Toggle Button -->
	<button
		onclick={toggleCollapsed}
		class="
			toggle-button absolute -left-3 top-4 w-6 h-6 rounded-full
			bg-card border border-border shadow-md
			flex items-center justify-center
			text-muted-foreground hover:text-foreground
			transition-all duration-200 hover:scale-110 z-10
		"
		aria-label={collapsed ? 'Expand panel' : 'Collapse panel'}
		aria-expanded={!collapsed}
	>
		<svg
			class="w-4 h-4 transition-transform duration-300"
			class:rotate-180={!collapsed}
			fill="none"
			stroke="currentColor"
			viewBox="0 0 24 24"
			aria-hidden="true"
		>
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
		</svg>
	</button>

	{#if !collapsed}
		<div class="panel-content flex-1 overflow-y-auto p-4 space-y-4">
			<!-- Active Threads Section -->
			<section class="section" aria-labelledby="threads-heading">
				<button
					onclick={() => toggleSection('threads')}
					class="section-header w-full flex items-center justify-between py-2 text-sm font-medium text-foreground hover:text-primary transition-colors"
					aria-expanded={!sectionsCollapsed.threads}
				>
					<span class="flex items-center gap-2" id="threads-heading">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="2"
								d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"
							/>
						</svg>
						Active Threads
						{#if activeSessionsCount > 0}
							<span
								class="px-1.5 py-0.5 text-[10px] font-bold rounded-full bg-primary/20 text-primary"
							>
								{activeSessionsCount}
							</span>
						{/if}
					</span>
					<svg
						class="w-4 h-4 transition-transform duration-200"
						class:rotate-180={!sectionsCollapsed.threads}
						fill="none"
						stroke="currentColor"
						viewBox="0 0 24 24"
					>
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</button>

				{#if !sectionsCollapsed.threads}
					<div class="section-content space-y-2 mt-2" role="group" aria-label="Thread list">
						{#if sessions.length === 0}
							<p class="text-xs text-muted-foreground py-2">No active threads</p>
						{:else}
							{#each sessions.slice(0, 5) as session}
								<button
									onclick={() => onSessionClick?.(session)}
									class="
										thread-item w-full text-left p-2 rounded-lg
										bg-secondary/50 hover:bg-accent
										transition-colors duration-150 group
									"
								>
									<div class="flex items-start justify-between gap-2">
										<span
											class="text-sm font-medium text-foreground truncate group-hover:text-primary"
										>
											{session.title}
										</span>
										{#if session.active}
											<span class="w-2 h-2 rounded-full bg-success shrink-0 mt-1.5"></span>
										{/if}
									</div>
									{#if session.preview}
										<p class="text-xs text-muted-foreground truncate mt-1">{session.preview}</p>
									{/if}
									<div class="flex items-center gap-2 mt-1 text-[10px] text-muted-foreground">
										<span>{formatTimeAgo(session.updatedAt)}</span>
										{#if session.tokenCount}
											<span class="opacity-50">|</span>
											<span>{formatTokens(session.tokenCount)} tokens</span>
										{/if}
									</div>
								</button>
							{/each}
						{/if}
					</div>
				{/if}
			</section>

			<!-- Running Agents Section -->
			<section class="section" aria-labelledby="agents-heading">
				<button
					onclick={() => toggleSection('agents')}
					class="section-header w-full flex items-center justify-between py-2 text-sm font-medium text-foreground hover:text-primary transition-colors"
					aria-expanded={!sectionsCollapsed.agents}
				>
					<span class="flex items-center gap-2" id="agents-heading">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="2"
								d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"
							/>
						</svg>
						Running Agents
						{#if runningAgentsCount > 0}
							<span
								class="px-1.5 py-0.5 text-[10px] font-bold rounded-full bg-success/20 text-success"
							>
								{runningAgentsCount}
							</span>
						{/if}
					</span>
					<svg
						class="w-4 h-4 transition-transform duration-200"
						class:rotate-180={!sectionsCollapsed.agents}
						fill="none"
						stroke="currentColor"
						viewBox="0 0 24 24"
					>
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</button>

				{#if !sectionsCollapsed.agents}
					<div class="section-content space-y-2 mt-2" role="group" aria-label="Agent list">
						{#if agents.length === 0}
							<p class="text-xs text-muted-foreground py-2">No agents running</p>
						{:else}
							{#each agents as agent}
								<button
									onclick={() => onAgentClick?.(agent)}
									class="
										agent-item w-full text-left p-2 rounded-lg
										bg-secondary/50 hover:bg-accent
										transition-colors duration-150 group
									"
								>
									<div class="flex items-center gap-2">
										<!-- Status indicator -->
										<span
											class="w-2 h-2 rounded-full {getStatusBgColor(agent.status)}"
											class:animate-pulse={agent.status === 'running'}
										></span>
										<span class="text-sm font-medium text-foreground truncate flex-1">
											{agent.name}
										</span>
										<span class="text-[10px] {getStatusColor(agent.status)} capitalize">
											{agent.status}
										</span>
									</div>
									{#if agent.currentTask}
										<p class="text-xs text-muted-foreground truncate mt-1 ml-4">
											{agent.currentTask}
										</p>
									{/if}
									{#if agent.progress !== undefined && agent.status === 'running'}
										<div class="mt-2 ml-4">
											<div class="h-1 bg-muted rounded-full overflow-hidden">
												<div
													class="h-full bg-primary transition-all duration-300"
													style="width: {agent.progress}%"
												></div>
											</div>
										</div>
									{/if}
									{#if agent.branch}
										<div class="flex items-center gap-1 mt-1 ml-4 text-[10px] text-muted-foreground">
											<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path
													stroke-linecap="round"
													stroke-linejoin="round"
													stroke-width="2"
													d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"
												/>
											</svg>
											<span class="font-mono">{agent.branch}</span>
										</div>
									{/if}
								</button>
							{/each}
						{/if}
					</div>
				{/if}
			</section>

			<!-- Generations Section -->
			<section class="section" aria-labelledby="generations-heading">
				<button
					onclick={() => toggleSection('generations')}
					class="section-header w-full flex items-center justify-between py-2 text-sm font-medium text-foreground hover:text-primary transition-colors"
					aria-expanded={!sectionsCollapsed.generations}
				>
					<span class="flex items-center gap-2" id="generations-heading">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="2"
								d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"
							/>
						</svg>
						Generations
						{#if pendingGenerationsCount > 0}
							<span
								class="px-1.5 py-0.5 text-[10px] font-bold rounded-full bg-warning/20 text-warning"
							>
								{pendingGenerationsCount}
							</span>
						{/if}
					</span>
					<svg
						class="w-4 h-4 transition-transform duration-200"
						class:rotate-180={!sectionsCollapsed.generations}
						fill="none"
						stroke="currentColor"
						viewBox="0 0 24 24"
					>
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</button>

				{#if !sectionsCollapsed.generations}
					<div class="section-content space-y-2 mt-2" role="group" aria-label="Generations list">
						{#if generations.length === 0}
							<p class="text-xs text-muted-foreground py-2">No generations</p>
						{:else}
							{#each generations as generation}
								<button
									onclick={() => onGenerationClick?.(generation)}
									class="
										generation-item w-full text-left p-2 rounded-lg
										bg-secondary/50 hover:bg-accent
										transition-colors duration-150 group
									"
								>
									<div class="flex items-center gap-2">
										<!-- Thumbnail or placeholder -->
										<div
											class="w-10 h-10 rounded-lg bg-muted flex items-center justify-center overflow-hidden shrink-0"
										>
											{#if generation.thumbnailUrl}
												<img
													src={generation.thumbnailUrl}
													alt=""
													class="w-full h-full object-cover"
												/>
											{:else if generation.status === 'generating' || generation.status === 'pending'}
												<svg
													class="w-5 h-5 text-muted-foreground animate-spin"
													fill="none"
													viewBox="0 0 24 24"
												>
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
											{:else}
												<svg
													class="w-5 h-5 text-muted-foreground"
													fill="none"
													stroke="currentColor"
													viewBox="0 0 24 24"
												>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														stroke-width="2"
														d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"
													/>
												</svg>
											{/if}
										</div>
										<div class="flex-1 min-w-0">
											<div class="flex items-center justify-between gap-2">
												<span class="text-xs font-medium capitalize">{generation.type}</span>
												<span class="text-[10px] {getStatusColor(generation.status)} capitalize">
													{generation.status}
												</span>
											</div>
											<p class="text-[10px] text-muted-foreground truncate mt-0.5">
												{generation.prompt}
											</p>
											{#if generation.progress !== undefined && (generation.status === 'generating' || generation.status === 'pending')}
												<div class="mt-1">
													<div class="h-1 bg-muted rounded-full overflow-hidden">
														<div
															class="h-full bg-primary transition-all duration-300"
															style="width: {generation.progress}%"
														></div>
													</div>
												</div>
											{/if}
										</div>
									</div>
								</button>
							{/each}
						{/if}
					</div>
				{/if}
			</section>

			<!-- Session Info Section -->
			{#if currentSession}
				<section class="section" aria-labelledby="session-heading">
					<button
						onclick={() => toggleSection('session')}
						class="section-header w-full flex items-center justify-between py-2 text-sm font-medium text-foreground hover:text-primary transition-colors"
						aria-expanded={!sectionsCollapsed.session}
					>
						<span class="flex items-center gap-2" id="session-heading">
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									stroke-width="2"
									d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"
								/>
							</svg>
							Session Info
						</span>
						<svg
							class="w-4 h-4 transition-transform duration-200"
							class:rotate-180={!sectionsCollapsed.session}
							fill="none"
							stroke="currentColor"
							viewBox="0 0 24 24"
						>
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="2"
								d="M19 9l-7 7-7-7"
							/>
						</svg>
					</button>

					{#if !sectionsCollapsed.session}
						<div class="section-content mt-2 p-3 rounded-lg bg-secondary/50 space-y-3">
							{#if currentSession.modelName}
								<div class="flex items-center justify-between text-xs">
									<span class="text-muted-foreground">Model</span>
									<span class="font-medium text-foreground">{currentSession.modelName}</span>
								</div>
							{/if}
							<div class="flex items-center justify-between text-xs">
								<span class="text-muted-foreground">Input Tokens</span>
								<span class="font-mono text-foreground">
									{formatTokens(currentSession.inputTokens)}
								</span>
							</div>
							<div class="flex items-center justify-between text-xs">
								<span class="text-muted-foreground">Output Tokens</span>
								<span class="font-mono text-foreground">
									{formatTokens(currentSession.outputTokens)}
								</span>
							</div>
							<div
								class="flex items-center justify-between text-xs pt-2 border-t border-border/50"
							>
								<span class="text-muted-foreground">Total Cost</span>
								<span class="font-medium text-primary">
									{formatCost(currentSession.totalCost)}
								</span>
							</div>
						</div>
					{/if}
				</section>
			{/if}
		</div>
	{/if}
</aside>

<style>
	.context-panel {
		width: 320px;
		position: relative;
	}

	.context-panel.collapsed {
		width: 0;
		border-left: none;
		overflow: hidden;
	}

	.context-panel.collapsed .toggle-button {
		left: -24px;
	}

	.panel-content {
		animation: fade-in 200ms ease-out;
	}

	@keyframes fade-in {
		from {
			opacity: 0;
		}
		to {
			opacity: 1;
		}
	}

	.section {
		border-bottom: 1px solid var(--border);
		padding-bottom: 0.75rem;
	}

	.section:last-child {
		border-bottom: none;
		padding-bottom: 0;
	}

	/* Mobile: Panel slides over */
	@media (max-width: 640px) {
		.context-panel {
			position: fixed;
			right: 0;
			top: 0;
			bottom: 56px;
			z-index: 40;
			width: 320px;
			max-width: calc(100vw - 48px);
			transform: translateX(0);
		}

		.context-panel.collapsed {
			transform: translateX(100%);
			width: 320px;
			border-left: 1px solid var(--border);
		}

		.toggle-button {
			left: -32px !important;
		}
	}
</style>
