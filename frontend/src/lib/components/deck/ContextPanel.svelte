<script lang="ts">
	/**
	 * ContextPanel - Right sidebar with collapsible sections
	 *
	 * Shows active threads, running agents, generations, and session info.
	 * Features glassmorphism styling and collapsible sections.
	 */

	import {
		ChevronLeft,
		ChevronRight,
		ChevronDown,
		MessageSquare,
		Bot,
		Image,
		Info,
		Circle
	} from 'lucide-svelte';
	import type { DeckSession, DeckAgent, DeckGeneration, SessionInfo } from './types';

	interface Props {
		collapsed?: boolean;
		sessions?: DeckSession[];
		agents?: DeckAgent[];
		generations?: DeckGeneration[];
		sessionInfo?: SessionInfo | null;
		onToggleCollapse?: () => void;
		onSessionClick?: (session: DeckSession) => void;
		onAgentClick?: (agent: DeckAgent) => void;
		onGenerationClick?: (generation: DeckGeneration) => void;
	}

	let {
		collapsed = false,
		sessions = [],
		agents = [],
		generations = [],
		sessionInfo = null,
		onToggleCollapse,
		onSessionClick,
		onAgentClick,
		onGenerationClick
	}: Props = $props();

	// Section collapse states
	let sectionsCollapsed = $state({
		threads: false,
		agents: false,
		generations: false,
		sessionInfo: false
	});

	function toggleSection(section: keyof typeof sectionsCollapsed) {
		sectionsCollapsed[section] = !sectionsCollapsed[section];
	}

	function getAgentStatusColor(status: DeckAgent['status']): string {
		switch (status) {
			case 'running':
				return '#10b981';
			case 'idle':
				return '#6b7280';
			case 'error':
				return '#ef4444';
			case 'paused':
				return '#f59e0b';
			default:
				return '#6b7280';
		}
	}

	function formatCost(cost: number): string {
		return cost < 0.01 ? '<$0.01' : `$${cost.toFixed(2)}`;
	}

	function formatTokens(tokens: number): string {
		if (tokens >= 1000000) return `${(tokens / 1000000).toFixed(1)}M`;
		if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K`;
		return tokens.toString();
	}

	function formatTime(date: Date): string {
		const now = new Date();
		const diff = now.getTime() - date.getTime();
		const minutes = Math.floor(diff / 60000);
		const hours = Math.floor(diff / 3600000);
		const days = Math.floor(diff / 86400000);

		if (minutes < 1) return 'just now';
		if (minutes < 60) return `${minutes}m ago`;
		if (hours < 24) return `${hours}h ago`;
		return `${days}d ago`;
	}
</script>

<div class="context-panel" class:collapsed>
	<!-- Collapse toggle button - only show when NOT collapsed (expanded state) -->
	{#if !collapsed}
		<button class="collapse-toggle" onclick={() => onToggleCollapse?.()} title="Collapse panel">
			<ChevronRight size={16} strokeWidth={2} />
		</button>
	{/if}

	{#if !collapsed}
		<div class="panel-content">
			<!-- Active Threads Section -->
			<div class="section">
				<button class="section-header" onclick={() => toggleSection('threads')}>
					<div class="section-title">
						<MessageSquare size={14} strokeWidth={1.5} />
						<span>Active Threads</span>
						{#if sessions.length > 0}
							<span class="section-count">{sessions.length}</span>
						{/if}
					</div>
					<span class="chevron" class:collapsed={sectionsCollapsed.threads}>
						<ChevronDown size={14} strokeWidth={2} />
					</span>
				</button>
				{#if !sectionsCollapsed.threads}
					<div class="section-content">
						{#if sessions.length === 0}
							<div class="empty-state">No active threads</div>
						{:else}
							{#each sessions as session}
								<button
									class="list-item"
									class:active={session.isActive}
									class:unread={session.unread}
									onclick={() => onSessionClick?.(session)}
								>
									<div class="item-content">
										<div class="item-title">{session.title}</div>
										{#if session.lastMessage}
											<div class="item-subtitle">{session.lastMessage}</div>
										{/if}
									</div>
									<div class="item-meta">{formatTime(session.timestamp)}</div>
								</button>
							{/each}
						{/if}
					</div>
				{/if}
			</div>

			<!-- Running Agents Section -->
			<div class="section">
				<button class="section-header" onclick={() => toggleSection('agents')}>
					<div class="section-title">
						<Bot size={14} strokeWidth={1.5} />
						<span>Running Agents</span>
						{#if agents.filter(a => a.status === 'running').length > 0}
							<span class="section-count active">{agents.filter(a => a.status === 'running').length}</span>
						{/if}
					</div>
					<span class="chevron" class:collapsed={sectionsCollapsed.agents}>
						<ChevronDown size={14} strokeWidth={2} />
					</span>
				</button>
				{#if !sectionsCollapsed.agents}
					<div class="section-content">
						{#if agents.length === 0}
							<div class="empty-state">No agents running</div>
						{:else}
							{#each agents as agent}
								<button class="list-item" onclick={() => onAgentClick?.(agent)}>
									<div class="status-dot" style:background={getAgentStatusColor(agent.status)}></div>
									<div class="item-content">
										<div class="item-title">{agent.name}</div>
										{#if agent.task}
											<div class="item-subtitle">{agent.task}</div>
										{/if}
									</div>
									{#if agent.progress !== undefined && agent.status === 'running'}
										<div class="progress-indicator">{agent.progress}%</div>
									{/if}
								</button>
							{/each}
						{/if}
					</div>
				{/if}
			</div>

			<!-- Generations Section -->
			<div class="section">
				<button class="section-header" onclick={() => toggleSection('generations')}>
					<div class="section-title">
						<Image size={14} strokeWidth={1.5} />
						<span>Generations</span>
						{#if generations.filter(g => g.status === 'generating').length > 0}
							<span class="section-count active">{generations.filter(g => g.status === 'generating').length}</span>
						{/if}
					</div>
					<span class="chevron" class:collapsed={sectionsCollapsed.generations}>
						<ChevronDown size={14} strokeWidth={2} />
					</span>
				</button>
				{#if !sectionsCollapsed.generations}
					<div class="section-content generations-grid">
						{#if generations.length === 0}
							<div class="empty-state">No generations</div>
						{:else}
							{#each generations as generation}
								<button
									class="generation-item"
									class:generating={generation.status === 'generating'}
									onclick={() => onGenerationClick?.(generation)}
								>
									{#if generation.thumbnailUrl}
										<img src={generation.thumbnailUrl} alt={generation.prompt} class="generation-thumbnail" />
									{:else}
										<div class="generation-placeholder">
											<Image size={20} strokeWidth={1.5} />
										</div>
									{/if}
									{#if generation.status === 'generating' && generation.progress !== undefined}
										<div class="generation-progress">
											<div class="progress-bar" style:width="{generation.progress}%"></div>
										</div>
									{/if}
								</button>
							{/each}
						{/if}
					</div>
				{/if}
			</div>

			<!-- Session Info Section -->
			<div class="section">
				<button class="section-header" onclick={() => toggleSection('sessionInfo')}>
					<div class="section-title">
						<Info size={14} strokeWidth={1.5} />
						<span>Session Info</span>
					</div>
					<span class="chevron" class:collapsed={sectionsCollapsed.sessionInfo}>
						<ChevronDown size={14} strokeWidth={2} />
					</span>
				</button>
				{#if !sectionsCollapsed.sessionInfo}
					<div class="section-content">
						{#if !sessionInfo}
							<div class="empty-state">No session selected</div>
						{:else}
							<div class="info-grid">
								<div class="info-item">
									<span class="info-label">Model</span>
									<span class="info-value">{sessionInfo.model}</span>
								</div>
								<div class="info-item">
									<span class="info-label">Tokens</span>
									<span class="info-value">
										{formatTokens(sessionInfo.tokens.input)} / {formatTokens(sessionInfo.tokens.output)}
									</span>
								</div>
								<div class="info-item">
									<span class="info-label">Cost</span>
									<span class="info-value">{formatCost(sessionInfo.cost)}</span>
								</div>
								{#if sessionInfo.duration}
									<div class="info-item">
										<span class="info-label">Duration</span>
										<span class="info-value">{Math.floor(sessionInfo.duration / 60)}m {sessionInfo.duration % 60}s</span>
									</div>
								{/if}
							</div>
						{/if}
					</div>
				{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	.context-panel {
		position: relative;
		width: 320px;
		height: 100%;
		background: var(--card);
		backdrop-filter: blur(24px);
		border-left: 1px solid var(--border);
		display: flex;
		flex-direction: column;
		transition: width 0.2s ease;
	}

	.context-panel.collapsed {
		width: 0;
		border-left: none;
		overflow: hidden;
	}

	.collapse-toggle {
		position: absolute;
		left: -12px;
		top: 50%;
		transform: translateY(-50%);
		width: 24px;
		height: 48px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 6px 0 0 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		transition: all 0.15s ease;
		z-index: 10;
	}

	.collapse-toggle:hover {
		color: var(--foreground);
		background: var(--accent);
		border-color: var(--border);
	}

	.panel-content {
		flex: 1;
		overflow-y: auto;
		padding: 12px;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.section {
		background: var(--secondary);
		border: 1px solid var(--border);
		border-radius: 10px;
		overflow: hidden;
	}

	.section-header {
		width: 100%;
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 10px 12px;
		background: transparent;
		border: none;
		color: var(--foreground);
		cursor: pointer;
		transition: background 0.15s ease;
	}

	.section-header:hover {
		background: var(--hover-overlay);
	}

	.section-title {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.section-count {
		padding: 2px 6px;
		background: var(--muted);
		border-radius: 10px;
		font-size: 0.625rem;
		font-weight: 700;
	}

	.section-count.active {
		background: color-mix(in oklch, var(--success) 20%, transparent);
		color: var(--success);
	}

	.chevron {
		display: flex;
		transition: transform 0.15s ease;
	}

	.chevron.collapsed {
		transform: rotate(-90deg);
	}

	.section-content {
		padding: 8px;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.empty-state {
		padding: 16px;
		text-align: center;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.list-item {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 8px 10px;
		background: transparent;
		border: 1px solid transparent;
		border-radius: 8px;
		cursor: pointer;
		transition: all 0.15s ease;
		text-align: left;
		width: 100%;
	}

	.list-item:hover {
		background: var(--hover-overlay);
		border-color: var(--border);
	}

	.list-item.active {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		border-color: color-mix(in oklch, var(--primary) 30%, transparent);
	}

	.list-item.unread::before {
		content: '';
		position: absolute;
		left: 4px;
		top: 50%;
		transform: translateY(-50%);
		width: 6px;
		height: 6px;
		background: var(--primary);
		border-radius: 50%;
	}

	.status-dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		flex-shrink: 0;
	}

	.item-content {
		flex: 1;
		min-width: 0;
	}

	.item-title {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.item-subtitle {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		margin-top: 2px;
	}

	.item-meta {
		font-size: 0.625rem;
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.progress-indicator {
		font-size: 0.625rem;
		font-weight: 600;
		color: var(--success);
		flex-shrink: 0;
	}

	.generations-grid {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 6px;
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
		border-color: #a855f7;
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
		background: linear-gradient(90deg, #a855f7, #ec4899);
		transition: width 0.3s ease;
	}

	.info-grid {
		display: grid;
		gap: 8px;
	}

	.info-item {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 6px 8px;
		background: var(--muted);
		border-radius: 6px;
		border: 1px solid var(--border);
	}

	.info-label {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.info-value {
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--foreground);
	}
</style>
