<script lang="ts">
	/** Analytics with shared panel navigation and token-coloured charts. */
	import { onMount } from 'svelte';
 import { platform } from '$lib/stores/platform.svelte';
	import {
		getAnalyticsSummary,
		getCostBreakdown,
		getUsageTrends,
		getTopSessions,
		getProviderBreakdown,
		exportAnalyticsCSV,
		type AnalyticsSummary,
		type CostBreakdownItem,
		type TrendDataPoint,
		type TopSession,
		type ProviderUsage
	} from '$lib/api/client';
	import { billingLabel, formatCost, formatTokens, providerCostCell, providerTotals } from './statsProviders';
	import { tick } from 'svelte';
	import { ViewShell, ViewPanel, PanelIconButton, ListItem, ListGroupLabel, DetailPane, Segmented, InlineBanner, EmptyDetail } from '$lib/components/layout/view';
	import UsageEstimatedBadge from '$lib/components/UsageEstimatedBadge.svelte';
	import { Download, RefreshCw, Loader2, ChevronRight } from 'lucide-svelte';

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
	let providers = $state<ProviderUsage[]>([]);
	let providersFailed = $state(false);

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
			const [summaryData, breakdownData, trendsData, sessionsData, providerData] = await Promise.all([
				getAnalyticsSummary(startDate, endDate).catch(() => null),
				getCostBreakdown(startDate, endDate, 'profile').catch(() => []),
				getUsageTrends(startDate, endDate).catch(() => []),
				getTopSessions(startDate, endDate, 5).catch(() => []),
				getProviderBreakdown(startDate, endDate).catch(() => null)
			]);

			summary = summaryData;
			costBreakdown = breakdownData;
			usageTrends = trendsData;
			topSessions = sessionsData;
			providers = providerData ?? [];
			providersFailed = providerData === null;
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

	const PRESETS: { id: PresetRange; label: string }[] = [
		{ id: '7d', label: '7 days' },
		{ id: '30d', label: '30 days' },
		{ id: '90d', label: '90 days' }
	];

	// Derived stats
	const totalTokens = $derived(summary ? summary.total_input_tokens + summary.total_output_tokens : 0);
	// Any turn in range with harness-estimated token counts (the provider
	// reported no usage, or the reply was interrupted) makes every aggregate
	// tile partly an estimate; the tiles then carry the "est." marker.
	const hasEstimates = $derived((summary?.estimated_query_count ?? 0) > 0);
	const maxProfileCost = $derived(Math.max(...costBreakdown.map((d) => d.cost), 0.0001));
	const maxTopCost = $derived(Math.max(...topSessions.map((s) => s.total_cost_usd), 0.0001));
	const maxProviderCost = $derived(Math.max(...providers.map((p) => p.total_cost_usd), 0.0001));
	const provTotals = $derived(providerTotals(providers));

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

 let detailOpen = $state(false);
 let stacked = $state(false);
 let contentEl = $state<HTMLDivElement>();
 const sections = [{id:'providers', label:'Usage by provider'}, {id:'spend',label:'Estimated spend over time'}, {id:'profiles',label:'Cost by profile'}, {id:'chats',label:'Top chats by cost'}];
 async function jump(id: string) {
  detailOpen = true;
  await tick();
  contentEl?.querySelector(`[data-section="${id}"]`)?.scrollIntoView({behavior:'smooth',block:'start'});
 }
</script>

<ViewShell label="Stats" contentFirst bind:stacked {detailOpen} onBack={() => detailOpen = false}>
 {#snippet panel()}
  <ViewPanel title="Stats" loading={loading || refreshing}>
   {#snippet actions()}
    <PanelIconButton label="Refresh" icon={RefreshCw} onclick={handleRefresh} disabled={refreshing || loading} />
    <PanelIconButton label="Export usage by provider as CSV" icon={exportLoading ? Loader2 : Download} onclick={handleExport} disabled={exportLoading} />
   {/snippet}
   <ListGroupLabel label="Range" compact />
   <div role="group" aria-label="Date range">
    {#each PRESETS as p}<ListItem variant="plain" id={p.id} name={p.label} selected={selectedPreset === p.id} onselect={() => { selectPreset(p.id); detailOpen = true; }} />{/each}
   </div>
   <ListGroupLabel label="Jump to" compact />
   {#each sections as section}<ListItem variant="plain" id={section.id} name={section.label} selected={false} onselect={() => jump(section.id)} />{/each}
  </ViewPanel>
 {/snippet}
 {#snippet main()}
  <DetailPane width="data" contentFirst name="Stats" subtitle={`${formatDisplayDate(startDate)} — ${formatDisplayDate(endDate)} · Usage, estimated spend and activity`}>
   {#if stacked}
    <div class="stats-controls">
     <Segmented label="Date range" options={PRESETS.map(p => ({value: p.id, label: p.label}))} value={selectedPreset} onchange={selectPreset} />
     {#if !platform.isMobile}
      <PanelIconButton label="Refresh" icon={RefreshCw} onclick={handleRefresh} disabled={refreshing || loading} />
      <PanelIconButton label="Export usage by provider as CSV" icon={exportLoading ? Loader2 : Download} onclick={handleExport} disabled={exportLoading} />
     {/if}
    </div>
   {/if}
   {#if error}<div role="alert"><InlineBanner>{error}</InlineBanner></div>{/if}
   {#if loading}<EmptyDetail title="Loading analytics" message="Gathering usage for this range…" />
   {:else}<div bind:this={contentEl}>
				<div class="metrics st-card">
					<!-- Metric cards -->
					<div class="metric">
						<div class="blabel" title="Estimated by this app from token counts and model prices. Provider-reported spend is in the usage popover.">Estimated spend {#if hasEstimates}<UsageEstimatedBadge label="est." />{/if}</div>
						<div class="bignum">{summary?.total_cost ? formatCost(summary.total_cost) : '$0.00'}</div>
						<div class="bsub">app estimate across {summary?.session_count ?? 0} sessions</div>
						{#if provTotals.unpriced > 0}
							<div class="bsub bsub--warn" title="These turns used a model with no known price; their cost is not in this total.">{provTotals.unpriced} {provTotals.unpriced === 1 ? 'turn has' : 'turns have'} no price</div>
						{/if}
					</div>

					<div class="metric">
						<div class="blabel">Tokens {#if hasEstimates}<UsageEstimatedBadge label="est." />{/if}</div>
						<div class="bignum">{formatTokens(totalTokens)}</div>
						<div class="bsub">{formatTokens(summary?.total_input_tokens ?? 0)} in · {formatTokens(summary?.total_output_tokens ?? 0)} out</div>
					</div>

					<div class="metric">
						<div class="blabel">Chats</div>
						<div class="bignum">{summary?.session_count ?? 0}</div>
						<div class="bsub">conversations in range</div>
					</div>

					<div class="metric">
						<div class="blabel">Avg / session {#if hasEstimates}<UsageEstimatedBadge label="est." />{/if}</div>
						<div class="bignum">{summary?.average_cost_per_session ? formatCost(summary.average_cost_per_session) : '$0.00'}</div>
						<div class="bsub">mean estimated spend per session</div>
					</div>

				</div>
					<!-- Usage by provider (full width) -->
					<section class="stats-section st-card" data-section="providers">
						<ListGroupLabel label="Usage by provider" />
						{#if providersFailed}
							<div class="bempty">Provider breakdown unavailable</div>
						{:else if providers.length === 0}
							<div class="bempty">No usage in this range</div>
						{:else}
							<div class="prov" role="table" aria-label="Usage by provider">
								<div class="prov-row prov-head" role="row">
									<span role="columnheader">Provider</span>
									<span role="columnheader" class="prov-num">Turns</span>
									<span role="columnheader" class="prov-num">Tokens</span>
									<span class="prov-trackcol" aria-hidden="true"></span>
									<span role="columnheader" class="prov-num">Est. cost</span>
								</div>
								{#each providers as p (p.key)}
									{@const cell = providerCostCell(p)}
									<div class="prov-row" role="row" data-provider={p.key}>
										<span class="prov-name" role="cell">
											<span class="prov-label" title={p.name}>{p.name}</span>
											<span class="prov-chip" data-billing={p.billing}>{billingLabel(p.billing)}</span>
											{#if p.estimated_query_count > 0}<UsageEstimatedBadge />{/if}
										</span>
										<span class="prov-num prov-turns" role="cell"><span class="prov-mlabel">Turns</span>{p.query_count.toLocaleString()}</span>
										<span class="prov-num prov-tokens" role="cell" title="{formatTokens(p.total_tokens_in)} in · {formatTokens(p.total_tokens_out)} out · {formatTokens(p.cache_read_tokens)} cache read"><span class="prov-mlabel">Tokens</span>{formatTokens(p.total_tokens_in + p.total_tokens_out)}</span>
										<span class="prov-track prov-trackcol" aria-hidden="true"><span class="prov-fill" class:sub={p.billing === 'subscription'} style="width:{cell.missing ? 0 : (p.total_cost_usd / maxProviderCost) * 100}%"></span></span>
										<span class="prov-cost" class:missing={cell.missing} role="cell" title={cell.title}>
											<span class="prov-cost-val">{cell.text}</span>
											{#if cell.note}<span class="prov-cost-note">{cell.note}</span>{/if}
										</span>
									</div>
								{/each}
							</div>
							{#if provTotals.inferred > 0}
								<p class="prov-foot">{provTotals.inferred.toLocaleString()} older {provTotals.inferred === 1 ? 'turn was' : 'turns were'} recorded before per-provider tracking and {provTotals.inferred === 1 ? 'is' : 'are'} shown under {provTotals.inferred === 1 ? 'its' : 'their'} session's current provider.</p>
							{/if}
						{/if}
					</section>

					<!-- Spend over time (tall, wide) -->
					<section class="stats-section st-card" data-section="spend">
						<ListGroupLabel label="Estimated spend over time" />
						{#if usageTrends.length === 0}
							<div class="bempty">No usage in this range</div>
						{:else}
							<div class="bars" role="img" aria-label="Daily spend">
								{#each usageTrends as d, i}
									<i
										class:acc={i === peakTrendIndex}
										style="height:{Math.max((d.cost / maxTrendCost) * 100, 3)}%"
										title="{formatDisplayDate(d.date)}: {formatCost(d.cost)}{d.estimated ? ' (includes estimated token counts)' : ''}"
									></i>
								{/each}
							</div>
						{/if}
					</section>

					<!-- Cost by profile -->
					<section class="stats-section st-card" data-section="profiles">
						<ListGroupLabel label="Cost by profile" />
						{#if costBreakdown.length === 0}
							<div class="bempty">No data</div>
						{:else}
							<div class="brk">
								{#each costBreakdown.slice(0, 5) as item}
									<div class="brk-row">
										<span class="brk-name">{item.name || 'Unknown'}{#if item.estimated} <UsageEstimatedBadge />{/if}</span>
										<span class="brk-track"><span class="brk-fill" style="width:{(item.cost / maxProfileCost) * 100}%"></span></span>
										<span class="brk-val">{formatCost(item.cost)}</span>
									</div>
								{/each}
							</div>
						{/if}
					</section>

					<!-- Top chats by cost -->
					<section class="stats-section st-card" data-section="chats">
						<ListGroupLabel label="Top chats by cost" />
						{#if topSessions.length === 0}
							<div class="bempty">No chats</div>
						{:else}
							<div class="tops">
								{#each topSessions.slice(0, 5) as s, i}
									<button class="top-row" onclick={() => handleSessionClick(s.id)}>
										<span class="top-rank">{i + 1}</span>
										<span class="top-name">{s.title || 'Untitled'}{#if s.usage_estimated} <UsageEstimatedBadge />{/if}</span>
										<span class="top-track"><span class="top-fill" style="width:{(s.total_cost_usd / maxTopCost) * 100}%"></span></span>
										<span class="top-val">{formatCost(s.total_cost_usd)}</span>
										<ChevronRight size={14} strokeWidth={1.8} class="top-arrow" />
									</button>
								{/each}
							</div>
						{/if}
					</section>
   </div>{/if}
  </DetailPane>
 {/snippet}
</ViewShell>
<style>
 .stats-controls { display: flex; align-items: center; gap: 4px; margin-bottom: 24px; flex-wrap: wrap; }
 .stats-controls :global(.st-seg) { margin-right: auto; }
 .stats-controls :global(.st-seg button) { min-height: 44px; padding-inline: 10px; }
 .metrics { display: grid; grid-template-columns: repeat(4, minmax(0,1fr)); }
 .metrics > .metric { min-width: 0; padding: 20px; border-radius: 0; }
 .metric + .metric { border-left: 1px solid var(--st-border); }
 .blabel { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin: 0 0 8px; color: var(--st-mfg); font-size: 12px; }
 .bignum { font-size: 28px; font-weight: 600; line-height: 1.2; font-variant-numeric: tabular-nums; }
 .bsub { font-size: 12px; color: var(--st-mfg); margin-top: 8px; }
 .bsub--warn { color: var(--st-warning); }
 .bempty { min-height: 80px; display: grid; place-items: center; color: var(--st-mfg); font-size: 13px; text-align: center; }
 .stats-section { scroll-margin-top: 24px; margin-top: 32px; padding: 20px; }
 .stats-section :global(.st-gl) { margin-top: 0; }
 .bars { height: 180px; }
 @container settings (max-width: 800px) { .metrics { grid-template-columns: 1fr 1fr; } .metric:nth-child(3) { border-left: 0; } .metric:nth-child(n+3) { border-top: 1px solid var(--st-border); } }
 @media (prefers-reduced-motion: reduce) { .bars i, .brk-fill, .prov-fill { transition: none; } }
	/* Spend-over-time bars */
	.bars { display: flex; align-items: flex-end; gap: 4px; flex: 1; min-height: 120px; margin-top: auto; padding-top: 14px; }
	.bars i {
		flex: 1; min-width: 2px; min-height: 3px;
		background: color-mix(in oklch, var(--foreground) 55%, transparent);
		border-radius: 4px 4px 0 0; transition: background-color 160ms ease;
	}
	.bars i.acc { background: var(--st-meter); }
	.bars:hover i { background: color-mix(in oklch, var(--foreground) 35%, transparent); }
	.bars i:hover { background: var(--st-fg); }

	/* Cost-by-profile rows */
	.brk { display: flex; flex-direction: column; gap: 11px; margin-top: 2px; }
	.brk-row { display: grid; grid-template-columns: minmax(64px, 96px) 1fr auto; align-items: center; gap: 10px; }
	.brk-name { font-size: 0.8125rem; color: var(--foreground); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.brk-track { height: 6px; background: var(--muted); border-radius: 999px; overflow: hidden; }
	.brk-fill { display: block; height: 100%; background: var(--st-meter); border-radius: 999px; transition: width 450ms ease; }
	.brk-val { font-size: 0.75rem; font-weight: 600; color: var(--muted-foreground); text-align: right; }

	/* Usage-by-provider rows */
	.prov { display: flex; flex-direction: column; }
	.prov-row {
		display: grid; grid-template-columns: minmax(0, 1.6fr) 64px 76px minmax(60px, 1fr) 128px;
		align-items: center; gap: 12px; padding: 8px 0;
		border-top: 1px solid color-mix(in oklch, var(--border) 60%, transparent);
	}
	.prov-head {
		border-top: none; padding: 0 0 4px;
		font-size: 12px; color: var(--muted-foreground);
	}
	.prov-name { display: flex; align-items: center; gap: 8px; min-width: 0; font-size: 0.8125rem; color: var(--foreground); }
	.prov-label { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.prov-chip {
		flex-shrink: 0; padding: 1px 7px; border-radius: 999px; font-size: 0.65rem; font-weight: 600;
		border: 1px solid var(--border); color: var(--muted-foreground); white-space: nowrap;
	}
	.prov-chip[data-billing='subscription'] { color: var(--st-mfg); }
	.prov-chip[data-billing='unknown'] { border-style: dashed; }
	.prov-num { font-size: 0.75rem; color: var(--muted-foreground); text-align: right; font-variant-numeric: tabular-nums; }
	.prov-head .prov-num { font-size: 12px; }
	.prov-mlabel { display: none; }
	.prov-track { height: 6px; background: var(--muted); border-radius: 999px; overflow: hidden; }
	.prov-fill { display: block; height: 100%; background: var(--st-meter); border-radius: 999px; transition: width 450ms ease; }
	/* Subscription equivalent remains labelled, with a quieter token-coloured track. */
	.prov-fill.sub { background: var(--st-meter); opacity: 0.65; }
	.prov-cost { display: flex; flex-direction: column; align-items: flex-end; text-align: right; cursor: help; }
	.prov-cost-val { font-size: 0.75rem; font-weight: 600; color: var(--foreground); }
	.prov-cost.missing .prov-cost-val { color: var(--muted-foreground); font-weight: 500; font-style: italic; }
	.prov-cost-note { font-size: 0.65rem; color: var(--muted-foreground); white-space: nowrap; }
	.prov-foot { margin: 10px 0 0; font-size: 0.72rem; color: var(--muted-foreground); }

	/* Top-sessions rows */
	.tops { display: flex; flex-direction: column; gap: 2px; margin: 0 -8px; }
	.top-row {
		display: grid; grid-template-columns: 20px minmax(0, 1fr) 56px auto 14px;
		align-items: center; gap: 10px; width: 100%;
		padding: 12px 8px; min-height: 44px; border: none; background: transparent; cursor: pointer;
		border-radius: calc(var(--radius, 0.75rem) - 4px); text-align: left;
		transition: background-color 120ms ease;
	}
	.top-row:hover { background: var(--hover-overlay); }
	.top-rank {
		display: grid; place-items: center; width: 20px; height: 20px;
		border-radius: 6px; background: color-mix(in oklch, var(--st-meter) 15%, transparent);
		color: var(--st-mfg); font-size: 0.65rem; font-weight: 600;
	}
	.top-name { font-size: 0.8125rem; color: var(--foreground); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.top-track { height: 5px; background: var(--muted); border-radius: 999px; overflow: hidden; }
	.top-fill { display: block; height: 100%; background: color-mix(in oklch, var(--st-meter) 75%, transparent); border-radius: 999px; }
	.top-val { font-size: 0.75rem; font-weight: 600; color: var(--muted-foreground); text-align: right; }
	.top-row :global(.top-arrow) { color: var(--muted-foreground); opacity: 0; transition: opacity 120ms ease; }
	.top-row:hover :global(.top-arrow) { opacity: 1; }
	/* Touch / mobile shell: the drill-in arrow can't be hover-revealed — show it so
	   the row reads as tappable. */
	@media (hover: none) {
		.top-row :global(.top-arrow) { opacity: 1; }
	}

	/* Mobile: single column */
	@media (max-width: 539px) {
		.metrics { grid-template-columns: 1fr 1fr; }
		.top-track { display: none; }
		.top-row { grid-template-columns: 20px minmax(0, 1fr) auto 14px; }
		/* Provider rows: name and cost on top, turns and tokens beneath. */
		.prov-head, .prov-trackcol { display: none; }
		.prov-row {
			grid-template-columns: auto minmax(0, 1fr) auto;
			grid-template-areas: 'name name cost' 'turns tokens cost';
			row-gap: 3px; column-gap: 14px;
		}
		.prov-name { grid-area: name; }
		.prov-turns { grid-area: turns; text-align: left; }
		.prov-tokens { grid-area: tokens; text-align: left; }
		.prov-cost { grid-area: cost; }
		.prov-mlabel { display: inline; margin-right: 4px; }
	}
</style>
