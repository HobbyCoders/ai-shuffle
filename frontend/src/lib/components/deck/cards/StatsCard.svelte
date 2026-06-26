<script lang="ts">
	/**
	 * StatsCard — the Studio-v2 Stats surface.
	 *
	 * Matches the mockup `#page-stats`: a Studio page header (eyebrow + serif title +
	 * accent + date-range segment / refresh / export actions) over a bento dashboard —
	 * four metric cards, a spend-over-time bar chart, cost-by-profile, and top sessions.
	 * Reuses the analytics API + all data/state logic; only the surface is new.
	 * Theme tokens only — never the mockup palette.
	 */
	import { onMount } from 'svelte';
	import { fade } from 'svelte/transition';
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
	import StudioPageHeader from '$lib/components/layout/StudioPageHeader.svelte';
	import { Download, RefreshCw, Loader2, Zap, DollarSign, MessageSquare, Gauge, ChevronRight } from 'lucide-svelte';

	interface Props {
		mobile?: boolean;
		onSessionClick?: (sessionId: string) => void;
	}

	let { mobile = false, onSessionClick }: Props = $props();

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

	// Date helpers
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
		if (!dateStr) return '';
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

	const PRESETS: { id: PresetRange; label: string }[] = [
		{ id: '7d', label: '7 days' },
		{ id: '30d', label: '30 days' },
		{ id: '90d', label: '90 days' }
	];

	// Derived stats
	const totalTokens = $derived(summary ? summary.total_input_tokens + summary.total_output_tokens : 0);
	const maxProfileCost = $derived(Math.max(...costBreakdown.map((d) => d.cost), 0.0001));
	const maxTopCost = $derived(Math.max(...topSessions.map((s) => s.total_cost_usd), 0.0001));

	// Spend-over-time bars: index of the highest-spend day (accented).
	const maxTrendCost = $derived(Math.max(...usageTrends.map((d) => d.cost), 0.0001));
	const peakTrendIndex = $derived.by(() => {
		let idx = -1;
		let peak = -1;
		usageTrends.forEach((d, i) => {
			if (d.cost > peak) {
				peak = d.cost;
				idx = i;
			}
		});
		return idx;
	});

	onMount(() => {
		initializeDates('30d');
		loadAnalytics();
	});
</script>

<div class="st" class:mobile>
	<div class="st-wrap">
		<div class="st-head">
			<StudioPageHeader eyebrow="Usage, spend & activity" title="Your" accent="stats">
				{#snippet actions()}
					<div class="st-seg" role="group" aria-label="Date range">
						{#each PRESETS as p}
							<button class="st-seg-btn" class:on={selectedPreset === p.id} onclick={() => selectPreset(p.id)}>
								{p.label}
							</button>
						{/each}
					</div>
					<button class="st-btn st-btn--icon" onclick={handleRefresh} disabled={refreshing} title="Refresh" aria-label="Refresh">
						<RefreshCw size={15} strokeWidth={1.9} class={refreshing ? 'st-spin' : ''} />
					</button>
					<button class="st-btn st-btn--icon" onclick={handleExport} disabled={exportLoading} title="Export CSV" aria-label="Export CSV">
						{#if exportLoading}<Loader2 size={15} class="st-spin" />{:else}<Download size={15} strokeWidth={1.9} />{/if}
					</button>
				{/snippet}
			</StudioPageHeader>
			<p class="st-sub">{formatDisplayDate(startDate)} — {formatDisplayDate(endDate)}</p>
		</div>

		<div class="st-scroll">
			{#if error}
				<div class="st-error" transition:fade={{ duration: 160 }}>{error}</div>
			{/if}

			{#if loading}
				<div class="st-loading"><Loader2 size={28} class="st-spin" /><p>Loading analytics…</p></div>
			{:else}
				<div class="bento">
					<!-- Metric cards -->
					<div class="bcard">
						<div class="blabel"><DollarSign size={14} strokeWidth={2} /> Total spend</div>
						<div class="bignum">{formatCost(summary?.total_cost ?? 0)}</div>
						<div class="bsub">across {summary?.session_count ?? 0} sessions</div>
					</div>

					<div class="bcard">
						<div class="blabel"><Zap size={14} strokeWidth={2} /> Tokens</div>
						<div class="bignum">{formatTokens(totalTokens)}</div>
						<div class="bsub">{formatTokens(summary?.total_input_tokens ?? 0)} in · {formatTokens(summary?.total_output_tokens ?? 0)} out</div>
					</div>

					<div class="bcard">
						<div class="blabel"><MessageSquare size={14} strokeWidth={2} /> Sessions</div>
						<div class="bignum">{summary?.session_count ?? 0}</div>
						<div class="bsub">conversations in range</div>
					</div>

					<div class="bcard">
						<div class="blabel"><Gauge size={14} strokeWidth={2} /> Avg / session</div>
						<div class="bignum">{formatCost(summary?.average_cost_per_session ?? 0)}</div>
						<div class="bsub">mean spend per session</div>
					</div>

					<!-- Spend over time (tall, wide) -->
					<div class="bcard c2 r2 bflex">
						<div class="blabel">Spend over time</div>
						{#if usageTrends.length === 0}
							<div class="bempty">No usage in this range</div>
						{:else}
							<div class="bars" role="img" aria-label="Daily spend">
								{#each usageTrends as d, i}
									<i
										class:acc={i === peakTrendIndex}
										style="height:{Math.max((d.cost / maxTrendCost) * 100, 3)}%"
										title="{formatDisplayDate(d.date)}: {formatCost(d.cost)}"
									></i>
								{/each}
							</div>
						{/if}
					</div>

					<!-- Cost by profile -->
					<div class="bcard c2 bflex">
						<div class="blabel">Cost by profile</div>
						{#if costBreakdown.length === 0}
							<div class="bempty">No data</div>
						{:else}
							<div class="brk">
								{#each costBreakdown.slice(0, 5) as item}
									<div class="brk-row">
										<span class="brk-name">{item.name || 'Unknown'}</span>
										<span class="brk-track"><span class="brk-fill" style="width:{(item.cost / maxProfileCost) * 100}%"></span></span>
										<span class="brk-val">{formatCost(item.cost)}</span>
									</div>
								{/each}
							</div>
						{/if}
					</div>

					<!-- Top sessions by cost -->
					<div class="bcard c2 bflex">
						<div class="blabel">Top sessions by cost</div>
						{#if topSessions.length === 0}
							<div class="bempty">No sessions</div>
						{:else}
							<div class="tops">
								{#each topSessions.slice(0, 5) as s, i}
									<button class="top-row" onclick={() => handleSessionClick(s.id)}>
										<span class="top-rank">{i + 1}</span>
										<span class="top-name">{s.title || 'Untitled'}</span>
										<span class="top-track"><span class="top-fill" style="width:{(s.total_cost_usd / maxTopCost) * 100}%"></span></span>
										<span class="top-val">{formatCost(s.total_cost_usd)}</span>
										<ChevronRight size={14} strokeWidth={1.8} class="top-arrow" />
									</button>
								{/each}
							</div>
						{/if}
					</div>
				</div>
			{/if}
		</div>
	</div>
</div>

<style>
	.st {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow: hidden;
		background: var(--background);
	}
	/* Centered, max-width container — matches the mockup's .wrap (not full-bleed). */
	.st-wrap {
		width: 100%;
		max-width: 1240px;
		margin: 0 auto;
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}
	.st-head { flex-shrink: 0; padding: 22px 28px 0; }
	.st-sub { margin: 8px 0 0; font-size: 0.8125rem; color: var(--muted-foreground); font-family: var(--font-mono, monospace); }

	.st-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 18px 28px 28px; }

	/* Header actions: segmented date range + icon buttons */
	.st-seg { display: inline-flex; padding: 3px; gap: 2px; background: var(--muted); border-radius: calc(var(--radius, 0.75rem) - 2px); }
	.st-seg-btn {
		height: 28px; padding: 0 12px; border: none; background: transparent;
		color: var(--muted-foreground); font-size: 0.78rem; font-weight: 500; cursor: pointer;
		border-radius: calc(var(--radius, 0.75rem) - 4px); transition: background-color 120ms ease, color 120ms ease;
	}
	.st-seg-btn:hover { color: var(--foreground); }
	.st-seg-btn.on { background: var(--card); color: var(--foreground); box-shadow: 0 1px 2px rgba(0,0,0,0.18); }

	.st-btn {
		display: inline-flex; align-items: center; justify-content: center; gap: 6px;
		height: 34px; padding: 0 12px; background: var(--card); color: var(--foreground);
		border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) - 2px);
		font-size: 0.8125rem; font-weight: 500; cursor: pointer;
		transition: background-color 120ms ease, border-color 120ms ease;
	}
	.st-btn:hover:not(:disabled) { background: var(--hover-overlay); }
	.st-btn:disabled { opacity: 0.55; cursor: not-allowed; }
	.st-btn--icon { width: 34px; padding: 0; color: var(--muted-foreground); }
	.st-btn--icon:hover:not(:disabled) { color: var(--foreground); }

	:global(.st-spin) { animation: st-spin 1s linear infinite; }
	@keyframes st-spin { to { transform: rotate(360deg); } }

	/* Error + loading */
	.st-error {
		margin-bottom: 14px; padding: 10px 12px;
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
		color: var(--destructive); font-size: 0.8125rem;
		border: 1px solid color-mix(in oklch, var(--destructive) 30%, transparent);
	}
	.st-loading { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 56px; color: var(--muted-foreground); }
	.st-loading :global(svg) { color: var(--primary); }

	/* ============================ BENTO ============================ */
	.bento {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 14px;
		grid-auto-rows: minmax(118px, auto);
	}
	.bcard {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		padding: 16px 18px;
		position: relative;
		overflow: hidden;
		transition: border-color 160ms ease, box-shadow 160ms ease;
	}
	.bcard:hover { border-color: color-mix(in oklch, var(--primary) 40%, var(--border)); }
	.bcard.c2 { grid-column: span 2; }
	.bcard.r2 { grid-row: span 2; }
	.bflex { display: flex; flex-direction: column; }

	.blabel {
		display: flex; align-items: center; gap: 6px;
		font-family: var(--font-mono, monospace);
		font-size: 0.65rem; letter-spacing: 0.08em; text-transform: uppercase;
		color: var(--muted-foreground); margin-bottom: 12px;
	}
	.blabel :global(svg) { opacity: 0.85; }

	.bignum {
		font-family: var(--font-display, Georgia, serif);
		font-weight: 800; font-size: 2.35rem; letter-spacing: -0.03em; line-height: 1;
		color: var(--foreground); font-variant-numeric: tabular-nums;
	}
	.bsub { margin-top: 10px; font-size: 0.75rem; color: var(--muted-foreground); }

	.bempty { margin: auto 0; padding: 18px 0; text-align: center; color: var(--muted-foreground); font-size: 0.8125rem; opacity: 0.7; }

	/* Spend-over-time bars */
	.bars { display: flex; align-items: flex-end; gap: 4px; flex: 1; min-height: 120px; margin-top: auto; padding-top: 14px; }
	.bars i {
		flex: 1; min-width: 2px; min-height: 3px;
		background: color-mix(in oklch, var(--foreground) 55%, transparent);
		border-radius: 4px 4px 0 0; transition: background-color 160ms ease;
	}
	.bars i.acc { background: var(--primary); }
	.bars:hover i { background: color-mix(in oklch, var(--foreground) 35%, transparent); }
	.bars i:hover { background: var(--primary) !important; }

	/* Cost-by-profile rows */
	.brk { display: flex; flex-direction: column; gap: 11px; margin-top: 2px; }
	.brk-row { display: grid; grid-template-columns: minmax(64px, 96px) 1fr auto; align-items: center; gap: 10px; }
	.brk-name { font-size: 0.8125rem; color: var(--foreground); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.brk-track { height: 6px; background: var(--muted); border-radius: 999px; overflow: hidden; }
	.brk-fill { display: block; height: 100%; background: var(--primary); border-radius: 999px; transition: width 450ms ease; }
	.brk-val { font-family: var(--font-mono, monospace); font-size: 0.75rem; font-weight: 600; color: var(--muted-foreground); text-align: right; }

	/* Top-sessions rows */
	.tops { display: flex; flex-direction: column; gap: 2px; margin: 0 -8px; }
	.top-row {
		display: grid; grid-template-columns: 20px minmax(0, 1fr) 56px auto 14px;
		align-items: center; gap: 10px; width: 100%;
		padding: 7px 8px; border: none; background: transparent; cursor: pointer;
		border-radius: calc(var(--radius, 0.75rem) - 4px); text-align: left;
		transition: background-color 120ms ease;
	}
	.top-row:hover { background: var(--hover-overlay); }
	.top-rank {
		display: grid; place-items: center; width: 20px; height: 20px;
		border-radius: 6px; background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary); font-size: 0.65rem; font-weight: 700; font-family: var(--font-mono, monospace);
	}
	.top-name { font-size: 0.8125rem; color: var(--foreground); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.top-track { height: 5px; background: var(--muted); border-radius: 999px; overflow: hidden; }
	.top-fill { display: block; height: 100%; background: color-mix(in oklch, var(--primary) 75%, transparent); border-radius: 999px; }
	.top-val { font-family: var(--font-mono, monospace); font-size: 0.75rem; font-weight: 600; color: var(--muted-foreground); text-align: right; }
	.top-row :global(.top-arrow) { color: var(--muted-foreground); opacity: 0; transition: opacity 120ms ease; }
	.top-row:hover :global(.top-arrow) { opacity: 1; }
	/* Touch / mobile-width: the drill-in arrow can't be hover-revealed — show it so
	   the row reads as tappable. (hover: none) covers foldables; 1080px = app cutoff. */
	@media (hover: none), (max-width: 1080px) {
		.top-row :global(.top-arrow) { opacity: 1; }
	}

	/* Tablet: 2-col grid */
	@media (max-width: 860px) {
		.bento { grid-template-columns: 1fr 1fr; }
		.bcard.c2 { grid-column: span 2; }
		.bcard.r2 { grid-row: auto; }
		.bars { min-height: 96px; }
	}

	/* Mobile: single column */
	@media (max-width: 539px) {
		.st-head { padding: 18px 16px 0; }
		.st-scroll { padding: 14px 16px 22px; }
		.bento { grid-template-columns: 1fr; }
		.bcard.c2 { grid-column: span 1; }
		.bignum { font-size: 2rem; }
		.top-track { display: none; }
		.top-row { grid-template-columns: 20px minmax(0, 1fr) auto 14px; }
	}
</style>
