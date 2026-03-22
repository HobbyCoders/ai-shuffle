<script lang="ts">
	/**
	 * StatsCard - Usage Statistics Dashboard
	 *
	 * A bold "Data Brutalism meets Luxury" aesthetic:
	 * - Monospace numbers for technical precision
	 * - Bento-box grid layout with asymmetric stat blocks
	 * - Dramatic gradients and glass effects
	 * - Subtle geometric patterns for depth
	 * - Animated transitions and hover states
	 */
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import { onMount } from 'svelte';
	import { fly, fade } from 'svelte/transition';
	import {
		getAnalyticsSummary,
		getCostBreakdown,
		getUsageTrends,
		getTopSessions,
		exportAnalyticsCSV,
		type AnalyticsSummary,
		type CostBreakdownItem,
		type TrendDataPoint,
		type TopSession
	} from '$lib/api/client';
	import { Download, RefreshCw, Loader2, TrendingUp, TrendingDown, Zap, DollarSign, MessageSquare, Activity, ChevronRight, Calendar, BarChart3 } from 'lucide-svelte';
	import './card-design-system.css';

	interface Props {
		mobile?: boolean;
		onSessionClick?: (sessionId: string) => void;
	}

	let {
		mobile = false,
		onSessionClick
	}: Props = $props();

	// Date range state
	let startDate = $state('');
	let endDate = $state('');
	type PresetRange = '7d' | '30d' | '90d';
	let selectedPreset = $state<PresetRange>('30d');

	// Data state
	let summary = $state<AnalyticsSummary | null>(null);
	let costBreakdown = $state<CostBreakdownItem[]>([]);
	let usageTrends = $state<TrendDataPoint[]>([]);
	let topSessions = $state<TopSession[]>([]);

	// Loading states
	let loading = $state(true);
	let exportLoading = $state(false);
	let refreshing = $state(false);

	// Error state
	let error = $state<string | null>(null);

	// Animation state
	let mounted = $state(false);

	// Initialize dates
	function initializeDates(preset: PresetRange) {
		const end = new Date();
		const start = new Date();
		const days = preset === '7d' ? 7 : preset === '30d' ? 30 : 90;
		start.setDate(end.getDate() - days);
		startDate = formatDate(start);
		endDate = formatDate(end);
	}

	function formatDate(date: Date): string {
		return date.toISOString().split('T')[0];
	}

	function formatDisplayDate(dateStr: string): string {
		const date = new Date(dateStr);
		return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
	}

	// Load all analytics data
	async function loadAnalytics() {
		if (!startDate || !endDate) return;
		error = null;
		loading = true;

		try {
			const [summaryData, breakdownData, trendsData, sessionsData] = await Promise.all([
				getAnalyticsSummary(startDate, endDate).catch(() => null),
				getCostBreakdown(startDate, endDate, 'profile').catch(() => []),
				getUsageTrends(startDate, endDate).catch(() => []),
				getTopSessions(startDate, endDate, 5).catch(() => [])
			]);

			summary = summaryData;
			costBreakdown = breakdownData;
			usageTrends = trendsData;
			topSessions = sessionsData;
		} catch (e) {
			error = 'Failed to load analytics data';
			console.error(e);
		} finally {
			loading = false;
		}
	}

	function selectPreset(preset: PresetRange) {
		selectedPreset = preset;
		initializeDates(preset);
		loadAnalytics();
	}

	async function handleExport() {
		exportLoading = true;
		try {
			await exportAnalyticsCSV(startDate, endDate);
		} catch (e) {
			error = 'Export failed';
		} finally {
			exportLoading = false;
		}
	}

	async function handleRefresh() {
		refreshing = true;
		await loadAnalytics();
		refreshing = false;
	}

	function handleSessionClick(sessionId: string) {
		onSessionClick?.(sessionId);
	}

	// Format helpers
	function formatTokens(n: number): string {
		if (n >= 1000000) return (n / 1000000).toFixed(2) + 'M';
		if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
		return n.toLocaleString();
	}

	function formatCost(cost: number): string {
		if (cost >= 100) return '$' + cost.toFixed(0);
		if (cost >= 1) return '$' + cost.toFixed(2);
		return '$' + cost.toFixed(4);
	}

	// Derived stats
	const totalTokens = $derived(summary ? summary.total_input_tokens + summary.total_output_tokens : 0);
	const maxCost = $derived(Math.max(...costBreakdown.map(d => d.cost), 0.0001));

	// Sparkline path generator
	function generateSparkline(data: TrendDataPoint[]): string {
		if (data.length < 2) return '';
		const maxVal = Math.max(...data.map(d => d.input_tokens + d.output_tokens), 1);
		const width = 100;
		const height = 32;
		const points = data.map((d, i) => {
			const x = (i / (data.length - 1)) * width;
			const y = height - ((d.input_tokens + d.output_tokens) / maxVal) * height;
			return `${x},${y}`;
		});
		return `M${points.join(' L')}`;
	}

	onMount(() => {
		initializeDates('30d');
		loadAnalytics();
		setTimeout(() => mounted = true, 50);
	});
</script>

{#snippet statsContent()}
	<div class="stats-container" class:mobile class:mounted>
		<!-- Decorative background pattern -->
		<div class="stats-bg-pattern"></div>

		<!-- Header -->
		<header class="stats-header">
			<div class="stats-title-row">
				<div class="stats-title">
					<Activity class="w-5 h-5" strokeWidth={2.5} />
					<span>Usage Analytics</span>
				</div>
				<div class="stats-actions">
					<button
						class="stats-btn"
						onclick={handleRefresh}
						disabled={refreshing}
						title="Refresh"
					>
						<RefreshCw class="w-4 h-4 {refreshing ? 'spin' : ''}" />
					</button>
					<button
						class="stats-btn"
						onclick={handleExport}
						disabled={exportLoading}
						title="Export CSV"
					>
						{#if exportLoading}
							<Loader2 class="w-4 h-4 spin" />
						{:else}
							<Download class="w-4 h-4" />
						{/if}
					</button>
				</div>
			</div>

			<!-- Date range selector -->
			<div class="date-selector">
				<div class="date-pills">
					{#each ['7d', '30d', '90d'] as preset}
						<button
							class="date-pill"
							class:active={selectedPreset === preset}
							onclick={() => selectPreset(preset as PresetRange)}
						>
							{preset === '7d' ? '7D' : preset === '30d' ? '30D' : '90D'}
						</button>
					{/each}
				</div>
				<div class="date-range">
					<Calendar class="w-3.5 h-3.5" />
					<span>{formatDisplayDate(startDate)} — {formatDisplayDate(endDate)}</span>
				</div>
			</div>
		</header>

		{#if error}
			<div class="stats-error" transition:fade={{ duration: 200 }}>
				{error}
			</div>
		{/if}

		<!-- Main content -->
		<div class="stats-content">
			{#if loading}
				<div class="stats-loading">
					<div class="loading-pulse"></div>
					<span>Loading analytics...</span>
				</div>
			{:else}
				<!-- Bento Grid Layout -->
				<div class="bento-grid" class:mobile>
					<!-- Total Tokens - Large hero card -->
					<div class="bento-card hero" style="--delay: 0">
						<div class="bento-card-inner">
							<div class="stat-label">
								<Zap class="w-4 h-4" />
								Total Tokens
							</div>
							<div class="stat-value mono">{formatTokens(totalTokens)}</div>
							<div class="stat-breakdown">
								<span class="in">{formatTokens(summary?.total_input_tokens ?? 0)} in</span>
								<span class="separator">/</span>
								<span class="out">{formatTokens(summary?.total_output_tokens ?? 0)} out</span>
							</div>
							<!-- Mini sparkline -->
							{#if usageTrends.length > 1}
								<svg class="sparkline" viewBox="0 0 100 32" preserveAspectRatio="none">
									<path d={generateSparkline(usageTrends)} fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
								</svg>
							{/if}
						</div>
					</div>

					<!-- Total Cost -->
					<div class="bento-card cost" style="--delay: 1">
						<div class="bento-card-inner">
							<div class="stat-label">
								<DollarSign class="w-4 h-4" />
								Total Cost
							</div>
							<div class="stat-value mono cost-value">{formatCost(summary?.total_cost ?? 0)}</div>
							<div class="stat-detail">USD spent</div>
						</div>
					</div>

					<!-- Sessions -->
					<div class="bento-card sessions" style="--delay: 2">
						<div class="bento-card-inner">
							<div class="stat-label">
								<MessageSquare class="w-4 h-4" />
								Sessions
							</div>
							<div class="stat-value mono">{summary?.session_count ?? 0}</div>
							<div class="stat-detail">conversations</div>
						</div>
					</div>

					<!-- Cost per Session -->
					<div class="bento-card avg" style="--delay: 3">
						<div class="bento-card-inner">
							<div class="stat-label">Avg / Session</div>
							<div class="stat-value mono small">{formatCost(summary?.average_cost_per_session ?? 0)}</div>
						</div>
					</div>

					<!-- Cost Breakdown -->
					<div class="bento-card breakdown" style="--delay: 4">
						<div class="bento-card-inner">
							<div class="section-header">
								<span>Cost by Profile</span>
							</div>
							{#if costBreakdown.length === 0}
								<div class="empty-state">No data</div>
							{:else}
								<div class="breakdown-list">
									{#each costBreakdown.slice(0, 4) as item, i}
										{@const barWidth = (item.cost / maxCost) * 100}
										<div class="breakdown-item" style="--i: {i}">
											<div class="breakdown-name">{item.name || 'Unknown'}</div>
											<div class="breakdown-bar-container">
												<div class="breakdown-bar" style="width: {barWidth}%"></div>
											</div>
											<div class="breakdown-cost mono">{formatCost(item.cost)}</div>
										</div>
									{/each}
								</div>
							{/if}
						</div>
					</div>

					<!-- Top Sessions -->
					<div class="bento-card top-sessions" style="--delay: 5">
						<div class="bento-card-inner">
							<div class="section-header">
								<span>Top Sessions</span>
							</div>
							{#if topSessions.length === 0}
								<div class="empty-state">No sessions</div>
							{:else}
								<div class="sessions-list">
									{#each topSessions.slice(0, 4) as session, i}
										<button
											class="session-item"
											style="--i: {i}"
											onclick={() => handleSessionClick(session.id)}
										>
											<span class="session-rank">{i + 1}</span>
											<span class="session-title">{session.title || 'Untitled'}</span>
											<span class="session-cost mono">{formatCost(session.total_cost_usd)}</span>
											<ChevronRight class="w-3.5 h-3.5 session-arrow" />
										</button>
									{/each}
								</div>
							{/if}
						</div>
					</div>
				</div>
			{/if}
		</div>
	</div>
{/snippet}

<FeaturePageLayout icon={BarChart3} title="Stats">
	{#snippet children()}
		{@render statsContent()}
	{/snippet}
</FeaturePageLayout>

<style>
	/* ============================================
	   STATS CONTAINER
	   ============================================ */
	.stats-container {
		display: flex;
		flex-direction: column;
		height: 100%;
		color: var(--card-foreground);
		position: relative;
		overflow: hidden;
	}

	.stats-container.mobile {
		border-radius: 0;
	}

	/* Background layer (for future patterns if needed) */
	.stats-bg-pattern {
		position: absolute;
		inset: 0;
		pointer-events: none;
		z-index: 0;
	}

	/* ============================================
	   HEADER
	   ============================================ */
	.stats-header {
		padding: 1rem 1.25rem;
		border-bottom: 1px solid var(--border);
		position: relative;
		z-index: 1;
		flex-shrink: 0;
	}

	.stats-title-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 0.75rem;
	}

	.stats-title {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-weight: 600;
		font-size: 1rem;
		letter-spacing: -0.01em;
	}

	.stats-title :global(svg) {
		color: var(--primary);
	}

	.stats-actions {
		display: flex;
		gap: 0.375rem;
	}

	.stats-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		border-radius: calc(var(--radius) * 0.67);
		background: var(--muted);
		color: var(--muted-foreground);
		border: 1px solid transparent;
		cursor: pointer;
		transition: all 180ms ease;
	}

	.stats-btn:hover:not(:disabled) {
		background: var(--hover-overlay);
		color: var(--foreground);
		border-color: var(--border);
	}

	.stats-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.stats-btn :global(.spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	/* Date Selector */
	.date-selector {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem 1rem;
		flex-wrap: wrap;
	}

	.date-pills {
		display: flex;
		gap: 0.25rem;
		padding: 3px;
		background: var(--muted);
		border-radius: calc(var(--radius) * 0.85);
	}

	.date-pill {
		padding: 0.375rem 0.875rem;
		font-size: 0.75rem;
		font-weight: 600;
		letter-spacing: 0.02em;
		border: none;
		border-radius: calc(var(--radius) * 0.6);
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 180ms ease;
	}

	.date-pill:hover {
		color: var(--foreground);
	}

	.date-pill.active {
		background: var(--background);
		color: var(--foreground);
		box-shadow: 0 1px 3px rgba(0,0,0,0.1);
	}

	.date-range {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		font-family: var(--font-mono);
	}

	/* ============================================
	   CONTENT
	   ============================================ */
	.stats-content {
		flex: 1;
		overflow-y: auto;
		padding: 1rem 1.25rem;
		position: relative;
		z-index: 1;
	}

	.stats-error {
		margin-bottom: 1rem;
		padding: 0.75rem 1rem;
		border-radius: calc(var(--radius) * 0.85);
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
		color: var(--destructive);
		font-size: 0.875rem;
		border: 1px solid color-mix(in oklch, var(--destructive) 30%, transparent);
	}

	.stats-loading {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: 4rem 2rem;
		gap: 1rem;
		color: var(--muted-foreground);
	}

	.loading-pulse {
		width: 40px;
		height: 40px;
		border-radius: 50%;
		background: var(--primary);
		animation: pulse 1.5s ease-in-out infinite;
	}

	@keyframes pulse {
		0%, 100% { opacity: 0.3; transform: scale(0.8); }
		50% { opacity: 1; transform: scale(1); }
	}

	/* ============================================
	   BENTO GRID
	   ============================================ */
	.bento-grid {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		grid-template-rows: auto auto auto;
		gap: 0.75rem;
	}

	/* Card placements - desktop (3 col) */
	.bento-card.hero { grid-column: span 2; }
	.bento-card.cost { grid-column: span 1; }
	.bento-card.sessions { grid-column: span 1; }
	.bento-card.avg { grid-column: span 1; }
	.bento-card.breakdown { grid-column: span 2; grid-row: span 1; }
	.bento-card.top-sessions { grid-column: span 3; }

	/* Tablet (2 col) */
	@media (max-width: 767px) {
		.bento-grid {
			grid-template-columns: 1fr 1fr;
		}
		.bento-card.hero { grid-column: span 2; }
		.bento-card.breakdown { grid-column: span 2; }
		.bento-card.top-sessions { grid-column: span 2; }
	}

	/* Mobile (1 col) */
	@media (max-width: 479px) {
		.bento-grid {
			grid-template-columns: 1fr;
		}
		.bento-card.hero,
		.bento-card.cost,
		.bento-card.sessions,
		.bento-card.avg,
		.bento-card.breakdown,
		.bento-card.top-sessions {
			grid-column: span 1;
		}
	}

	/* Base card styling */
	.bento-card {
		border-radius: var(--radius);
		background: var(--surface-0);
		border: 1px solid var(--border-subtle);
		overflow: hidden;
		transition: all 250ms ease;
		opacity: 0;
		transform: translateY(8px);
	}

	.mounted .bento-card {
		opacity: 1;
		transform: translateY(0);
		transition-delay: calc(var(--delay, 0) * 60ms);
	}

	.bento-card:hover {
		border-color: var(--border-emphasis);
		box-shadow: var(--shadow-sm);
	}

	.bento-card-inner {
		padding: 1rem;
		height: 100%;
		display: flex;
		flex-direction: column;
	}

	/* Stat labels and values */
	.stat-label {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		margin-bottom: 0.5rem;
	}

	.stat-label :global(svg) {
		opacity: 0.7;
	}

	.stat-value {
		font-size: 2rem;
		font-weight: 700;
		line-height: 1;
		color: var(--foreground);
		letter-spacing: -0.02em;
	}

	.stat-value.small {
		font-size: 1.5rem;
	}

	.stat-value.mono, .mono {
		font-family: var(--font-mono);
		font-feature-settings: 'tnum' 1;
	}

	.stat-value.cost-value {
		background: linear-gradient(135deg, var(--success) 0%, color-mix(in oklch, var(--success) 70%, var(--primary)) 100%);
		-webkit-background-clip: text;
		-webkit-text-fill-color: transparent;
		background-clip: text;
	}

	.stat-detail {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		margin-top: 0.25rem;
	}

	.stat-breakdown {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.75rem;
		margin-top: 0.5rem;
		font-family: var(--font-mono);
	}

	.stat-breakdown .in {
		color: var(--info);
	}

	.stat-breakdown .out {
		color: var(--warning);
	}

	.stat-breakdown .separator {
		color: var(--muted-foreground);
		opacity: 0.5;
	}

	/* Sparkline */
	.sparkline {
		margin-top: auto;
		height: 32px;
		width: 100%;
		color: var(--primary);
		opacity: 0.6;
	}

	/* Section headers */
	.section-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--muted-foreground);
		margin-bottom: 0.75rem;
	}

	.empty-state {
		color: var(--muted-foreground);
		font-size: 0.875rem;
		text-align: center;
		padding: 1.5rem;
		opacity: 0.6;
	}

	/* ============================================
	   BREAKDOWN LIST
	   ============================================ */
	.breakdown-list {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.breakdown-item {
		display: grid;
		grid-template-columns: minmax(60px, 90px) 1fr auto;
		align-items: center;
		gap: 0.5rem;
		opacity: 0;
		animation: slideIn 300ms ease forwards;
		animation-delay: calc(var(--i, 0) * 50ms + 200ms);
	}

	.mounted .breakdown-item {
		opacity: 1;
	}

	@keyframes slideIn {
		from { opacity: 0; transform: translateX(-8px); }
		to { opacity: 1; transform: translateX(0); }
	}

	.breakdown-name {
		font-size: 0.8125rem;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.breakdown-bar-container {
		height: 6px;
		background: var(--muted);
		border-radius: calc(var(--radius) * 0.25);
		overflow: hidden;
	}

	.breakdown-bar {
		height: 100%;
		background: var(--primary);
		border-radius: calc(var(--radius) * 0.25);
		transition: width 500ms ease;
	}

	.breakdown-cost {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		text-align: right;
	}

	/* ============================================
	   SESSIONS LIST
	   ============================================ */
	.sessions-list {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.session-item {
		display: flex;
		align-items: center;
		gap: 0.625rem;
		padding: 0.5rem 0.625rem;
		border-radius: calc(var(--radius) * 0.67);
		background: transparent;
		border: none;
		color: var(--foreground);
		cursor: pointer;
		transition: all 180ms ease;
		text-align: left;
		width: 100%;
		opacity: 0;
		animation: slideIn 300ms ease forwards;
		animation-delay: calc(var(--i, 0) * 50ms + 300ms);
	}

	.mounted .session-item {
		opacity: 1;
	}

	.session-item:hover {
		background: var(--muted);
	}

	.session-rank {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 20px;
		border-radius: calc(var(--radius) * 0.4);
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
		font-size: 0.6875rem;
		font-weight: 700;
		flex-shrink: 0;
	}

	.session-title {
		flex: 1;
		font-size: 0.8125rem;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.session-cost {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	/* ============================================
	   MOBILE ADJUSTMENTS
	   ============================================ */
	@media (max-width: 639px) {
		.stats-header {
			padding: 0.75rem 1rem;
		}

		.stats-content {
			padding: 0.75rem 1rem;
		}

		.stat-value {
			font-size: 1.5rem;
		}

		.stat-value.small {
			font-size: 1.25rem;
		}

		.bento-card-inner {
			padding: 0.875rem;
		}

		.stat-breakdown {
			flex-wrap: wrap;
			gap: 0.25rem;
		}

		.stat-breakdown .separator {
			display: none;
		}
	}

	/* Touch targets */
	@media (pointer: coarse) {
		.stats-btn {
			width: 44px;
			height: 44px;
		}

		.date-pill {
			padding: 0.5rem 1rem;
			min-height: 44px;
		}

		.session-item {
			min-height: 48px;
		}
	}
</style>
