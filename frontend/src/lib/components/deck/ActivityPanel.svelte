<script lang="ts">
	/**
	 * ActivityPanel - Unified activity and settings panel
	 *
	 * Architecture:
	 * ┌─────────────────────────────────────┐
	 * │  ACTIVE SESSIONS (Fixed Header)     │  ← Always visible
	 * ├─────────────────────────────────────┤
	 * │  [Threads] [Agents] [Studio] [Info] │  ← Tab Bar
	 * ├─────────────────────────────────────┤
	 * │  TAB CONTENT AREA (scrollable)      │  ← Changes per tab
	 * ├─────────────────────────────────────┤
	 * │  OVERLAY (when active)              │  ← Slides up for settings
	 * └─────────────────────────────────────┘
	 */

	import { ChevronRight } from 'lucide-svelte';
	import ActivityHeader from './ActivityHeader.svelte';
	import ActivityTabs from './ActivityTabs.svelte';
	import ActivityOverlay from './ActivityOverlay.svelte';
	import ThreadsTabContent from './tabs/ThreadsTabContent.svelte';
	import AgentsTabContent from './tabs/AgentsTabContent.svelte';
	import StudioTabContent from './tabs/StudioTabContent.svelte';
	import InfoTabContent from './tabs/InfoTabContent.svelte';
	import type { DeckSession, DeckAgent, DeckGeneration, SessionInfo } from './types';

	// Active session for unified display
	export interface ActiveSession {
		id: string;
		type: 'chat' | 'agent';
		title: string;
		status: 'active' | 'idle' | 'streaming' | 'running' | 'error';
		progress?: number;
		isSelected: boolean;
		unread?: boolean;
	}

	// History session for loading old chats
	export interface HistorySession {
		id: string;
		title: string;
		timestamp: Date;
		isOpen?: boolean;
	}

	export type ActivityTabType = 'threads' | 'agents' | 'studio' | 'info';
	export type OverlayType = 'chat-settings' | null;

	interface Props {
		collapsed?: boolean;
		activeTab?: ActivityTabType;

		// Fixed header data
		activeSessions?: ActiveSession[];

		// Tab content
		recentSessions?: HistorySession[];
		runningAgents?: DeckAgent[];
		completedAgents?: DeckAgent[];
		generations?: DeckGeneration[];
		sessionInfo?: SessionInfo | null;

		// Overlay
		overlayType?: OverlayType;
		overlayData?: Record<string, unknown>;

		// Callbacks
		onToggleCollapse?: () => void;
		onTabChange?: (tab: ActivityTabType) => void;
		onSessionClick?: (session: ActiveSession) => void;
		onHistorySessionClick?: (session: HistorySession) => void;
		onAgentClick?: (agent: DeckAgent) => void;
		onGenerationClick?: (generation: DeckGeneration) => void;
		onOverlayClose?: () => void;
	}

	let {
		collapsed = false,
		activeTab = 'threads',
		activeSessions = [],
		recentSessions = [],
		runningAgents = [],
		completedAgents = [],
		generations = [],
		sessionInfo = null,
		overlayType = null,
		overlayData = {},
		onToggleCollapse,
		onTabChange,
		onSessionClick,
		onHistorySessionClick,
		onAgentClick,
		onGenerationClick,
		onOverlayClose
	}: Props = $props();

	// Derived values
	const hasActiveOverlay = $derived(overlayType !== null);
	const runningAgentCount = $derived(runningAgents.filter(a => a.status === 'running').length);
	const generatingCount = $derived(generations.filter(g => g.status === 'generating').length);

	// Tab badges
	const tabBadges = $derived({
		threads: activeSessions.filter(s => s.unread).length || undefined,
		agents: runningAgentCount || undefined,
		studio: generatingCount || undefined,
		info: undefined
	});

	function handleTabChange(tab: ActivityTabType) {
		onTabChange?.(tab);
	}
</script>

<div class="activity-panel" class:collapsed>
	<!-- Collapse toggle button -->
	{#if !collapsed}
		<button class="collapse-toggle" onclick={() => onToggleCollapse?.()} title="Collapse panel">
			<ChevronRight size={16} strokeWidth={2} />
		</button>
	{/if}

	{#if !collapsed}
		<div class="panel-content">
			{#if hasActiveOverlay}
				<!-- Full-screen overlay mode -->
				<ActivityOverlay
					type={overlayType}
					data={overlayData}
					onClose={onOverlayClose}
				/>
			{:else}
				<!-- Normal panel mode -->
				<!-- Fixed Header: Active Sessions -->
				<ActivityHeader
					sessions={activeSessions}
					onSessionClick={onSessionClick}
				/>

				<!-- Tab Navigation -->
				<ActivityTabs
					{activeTab}
					badges={tabBadges}
					onTabChange={handleTabChange}
				/>

				<!-- Tab Content Area -->
				<div class="tab-content">
					{#if activeTab === 'threads'}
						<ThreadsTabContent
							{recentSessions}
							{onHistorySessionClick}
						/>
					{:else if activeTab === 'agents'}
						<AgentsTabContent
							running={runningAgents}
							completed={completedAgents}
							{onAgentClick}
						/>
					{:else if activeTab === 'studio'}
						<StudioTabContent
							{generations}
							{onGenerationClick}
						/>
					{:else if activeTab === 'info'}
						<InfoTabContent
							{sessionInfo}
						/>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.activity-panel {
		position: relative;
		width: 100%;
		height: 100%;
		background: var(--card);
		backdrop-filter: blur(24px);
		-webkit-backdrop-filter: blur(24px);
		border-left: 1px solid var(--border);
		display: flex;
		flex-direction: column;
		overflow: visible;
	}

	.activity-panel.collapsed {
		border-left: none;
		overflow: hidden;
	}

	.collapse-toggle {
		position: absolute;
		left: 0;
		top: 50%;
		transform: translate(-100%, -50%);
		width: 28px;
		height: 52px;
		background: var(--card);
		border: 1px solid var(--border);
		border-right: none;
		border-radius: 8px 0 0 8px;
		color: var(--muted-foreground);
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		transition: all 0.15s ease;
		z-index: 10;
		min-width: 44px;
		min-height: 44px;
	}

	.collapse-toggle:hover {
		color: var(--foreground);
		background: var(--accent);
		border-color: var(--border);
	}

	.collapse-toggle:focus-visible {
		outline: 2px solid var(--primary);
		outline-offset: 2px;
	}

	.panel-content {
		flex: 1;
		display: flex;
		flex-direction: column;
		overflow: hidden;
		min-height: 0;
		position: relative;
	}

	.tab-content {
		flex: 1;
		overflow-y: auto;
		overflow-x: hidden;
		padding: 12px;
		min-height: 0;
	}
</style>
