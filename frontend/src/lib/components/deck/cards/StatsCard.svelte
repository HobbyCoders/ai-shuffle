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
	import { formatScale, labelIndexes, spendScale } from './statsChart';
	import { tick } from 'svelte';
	import { ViewShell, ViewPanel, PanelIconButton, ListItem, ListGroupLabel, DetailPane, Segmented, InlineBanner, EmptyDetail } from '$lib/components/layout/view';
	import UsageEstimatedBadge from '$lib/components/UsageEstimatedBadge.svelte';
	import { Download, RefreshCw, Loader2, ChevronRight, BarChart3 } from 'lucide-svelte';

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
		// A bare YYYY-MM-DD is a calendar day: read it as local midnight, not UTC.
		const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr);
		const date = m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(dateStr);
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

	// Spend-over-time bars: a rounded currency scale, a few date ticks, and
	// today (the range end) drawn at full accent so the one darker bar is named.
	const maxTrendCost = $derived(Math.max(...usageTrends.map((d) => d.cost), 0));
	const scale = $derived(spendScale(maxTrendCost));
	const todayIndex = $derived(usageTrends.findIndex((d) => d.date === endDate));
	const tickIndexes = $derived(new Set(labelIndexes(usageTrends.length)));
	/** With five ticks, the 2nd and 4th drop out when the chart is narrow. */
	const minorTicks = $derived.by(() => {
		const all = labelIndexes(usageTrends.length);
		return all.length === 5 ? new Set([all[1], all[3]]) : new Set<number>();
	});
	/** The hovered, focused or tapped day; null shows today (or the last day). */
	let pickedDay = $state<number | null>(null);
	const restingDay = $derived(todayIndex >= 0 ? todayIndex : usageTrends.length - 1);
	const shownDay = $derived(pickedDay ?? restingDay);
	let barEls = $state<HTMLButtonElement[]>([]);
	function dayLabel(i: number): string {
		return i === todayIndex ? 'Today' : formatDisplayDate(usageTrends[i]?.date ?? '');
	}
	function barLabel(i: number): string {
		const d = usageTrends[i];
		if (!d) return '';
		return `${i === todayIndex ? 'Today, ' : ''}${formatDisplayDate(d.date)}: ${formatCost(d.cost)}${d.estimated ? ', includes estimated token counts' : ''}`;
	}
	function barKey(e: KeyboardEvent, i: number) {
		const last = usageTrends.length - 1;
		const next = e.key === 'ArrowRight' || e.key === 'ArrowUp' ? Math.min(i + 1, last)
			: e.key === 'ArrowLeft' || e.key === 'ArrowDown' ? Math.max(i - 1, 0)
			: e.key === 'Home' ? 0 : e.key === 'End' ? last : null;
		if (next === null) return;
		e.preventDefault();
		pickedDay = next;
		barEls[next]?.focus();
	}
	function chartFocusOut(e: FocusEvent) {
		const to = e.relatedTarget as Node | null;
		if (!to || !(e.currentTarget as HTMLElement).contains(to)) pickedDay = null;
	}

	// Nothing at all in range: one page-level empty state instead of four empty cards.
	const trendsEmpty = $derived(!usageTrends.some((d) => d.cost > 0 || d.input_tokens > 0 || d.output_tokens > 0));
	const providersEmpty = $derived(!providersFailed && providers.length === 0);
	const allEmpty = $derived(providersEmpty && trendsEmpty && costBreakdown.length === 0 && topSessions.length === 0);
	const rangeDays = $derived(parseInt(selectedPreset));

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
  const target = contentEl?.querySelector(`[data-section="${id}"]`) ?? contentEl?.querySelector('[data-section="empty"]');
  target?.scrollIntoView({behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block:'start'});
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
  <DetailPane width="data" contentFirst name={`Last ${parseInt(selectedPreset)} days`} subtitle={`${formatDisplayDate(startDate)} — ${formatDisplayDate(endDate)} · Usage, estimated spend and activity`}>
   {#snippet heading()}<h2 class="stats-title">Last {parseInt(selectedPreset)} days</h2>{/snippet}
   {#if stacked}
    <div class="stats-controls">
     <Segmented label="Date range" options={PRESETS.map(p => ({value: p.id, label: p.label}))} value={selectedPreset} onchange={selectPreset} />

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
					{#if allEmpty}
						<div class="stats-empty" data-section="empty">
							<EmptyDetail icon={BarChart3} title={`No usage in the last ${rangeDays} days`} message="Usage and estimated spend show here once chats run in this range.">
								{#if selectedPreset !== '90d'}<button type="button" class="st-btn" onclick={() => selectPreset('90d')}>Show 90 days</button>{/if}
							</EmptyDetail>
						</div>
					{:else}
					{#snippet emptyRow(id: string, title: string, line: string)}
						<section class="stats-section is-empty" data-section={id} aria-labelledby="stats-{id}">
							<div class="stats-empty-row st-card"><h3 id="stats-{id}">{title}</h3><span>{line}</span></div>
						</section>
					{/snippet}
						<!-- Usage by provider (full width) -->
						{#if providersEmpty}{@render emptyRow('providers', 'Usage by provider', 'No usage in this range')}
						{:else}
						<section class="stats-section" data-section="providers" aria-labelledby="stats-providers">
							<h3 id="stats-providers">Usage by provider</h3>
							<div class="stats-content st-card">
							{#if providersFailed}
								<div class="bempty">Provider breakdown unavailable</div>
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
												{#if p.estimated_query_count > 0}<UsageEstimatedBadge label="Estimated" />{/if}
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
							</div>
						</section>
						{/if}

						<!-- Spend over time: currency scale, gridlines, date ticks, a readout for the picked day -->
						{#if trendsEmpty}{@render emptyRow('spend', 'Estimated spend over time', 'No spend in this range')}
						{:else}
						{@const shown = usageTrends[shownDay]}
						<section class="stats-section" data-section="spend" aria-labelledby="stats-spend">
							<h3 id="stats-spend">Estimated spend over time</h3>
							<div class="stats-content st-card">
								<div class="chart" style="--days:{usageTrends.length}">
									<p class="chart-readout" aria-hidden="true">
										{#if shown}<span class="chart-readout-day">{shownDay === todayIndex ? `Today, ${formatDisplayDate(shown.date)}` : formatDisplayDate(shown.date)}</span>
										<span class="chart-readout-val">{formatCost(shown.cost)}</span>{#if shown.estimated}<UsageEstimatedBadge label="Estimated" />{/if}{/if}
									</p>
									<div class="chart-body">
										<div class="chart-y" aria-hidden="true">
											<span class="chart-y-sizer">{formatScale(scale.top)}</span>
											<span style="bottom:0">{formatScale(0)}</span>
											{#each scale.ticks as t}<span style="bottom:{(t / scale.top) * 100}%">{formatScale(t)}</span>{/each}
										</div>
										<div class="chart-plot">
											<div class="chart-grid" aria-hidden="true">
												{#each scale.ticks as t}<i style="bottom:{(t / scale.top) * 100}%"></i>{/each}
											</div>
											<!-- svelte-ignore a11y_no_static_element_interactions -->
											<div class="chart-bars" role="group" aria-label="Daily spend, {formatDisplayDate(usageTrends[0]?.date)} to {formatDisplayDate(usageTrends[usageTrends.length - 1]?.date)}. Use the arrow keys to move between days." onmouseleave={() => { if (!barEls.includes(document.activeElement as HTMLButtonElement)) pickedDay = null; }} onfocusout={chartFocusOut}>
												{#each usageTrends as d, i}
													<button
														type="button"
														class="chart-col"
														class:today={i === todayIndex}
														class:picked={pickedDay === i}
														bind:this={barEls[i]}
														tabindex={i === shownDay ? 0 : -1}
														aria-label={barLabel(i)}
														onmouseenter={() => pickedDay = i}
														onfocus={() => pickedDay = i}
														onclick={() => pickedDay = i}
														onkeydown={(e) => barKey(e, i)}
													><span class="chart-bar" style="height:{d.cost > 0 ? Math.max((d.cost / scale.top) * 100, 1.5) : 0}%"></span></button>
												{/each}
											</div>
										</div>
										<div class="chart-x" aria-hidden="true">
											{#each usageTrends as _, i}
												{#if tickIndexes.has(i)}<span class:first={i === 0} class:last={i === usageTrends.length - 1} class:minor={minorTicks.has(i)} class:today={i === todayIndex} style="left:{((i + 0.5) / usageTrends.length) * 100}%">{dayLabel(i)}</span>{/if}
											{/each}
										</div>
									</div>
								</div>
							</div>
						</section>
						{/if}

						<!-- Cost by profile -->
						{#if costBreakdown.length === 0}{@render emptyRow('profiles', 'Cost by profile', 'No profile costs in this range')}
						{:else}
						<section class="stats-section" data-section="profiles" aria-labelledby="stats-profiles">
							<h3 id="stats-profiles">Cost by profile</h3>
							<div class="stats-content st-card">
								<div class="brk">
									{#each costBreakdown.slice(0, 5) as item}
										<div class="brk-row">
											<span class="brk-name"><span class="brk-label" title={item.name || 'Unknown'}>{item.name || 'Unknown'}</span>{#if item.estimated}<UsageEstimatedBadge label="Estimated" />{/if}</span>
											<span class="brk-track"><span class="brk-fill" style="width:{(item.cost / maxProfileCost) * 100}%"></span></span>
											<span class="brk-val">{formatCost(item.cost)}</span>
										</div>
									{/each}
								</div>
							</div>
						</section>
						{/if}

						<!-- Top chats by cost -->
						{#if topSessions.length === 0}{@render emptyRow('chats', 'Top chats by cost', 'No chats')}
						{:else}
						<section class="stats-section" data-section="chats" aria-labelledby="stats-chats">
							<h3 id="stats-chats">Top chats by cost</h3>
							<div class="stats-content st-card">
								<div class="tops">
									{#each topSessions.slice(0, 5) as s, i}
										<button class="top-row" onclick={() => handleSessionClick(s.id)}>
											<span class="top-rank">{i + 1}</span>
											<span class="top-name"><span class="top-label">{s.title || 'Untitled'}</span>{#if s.usage_estimated}<UsageEstimatedBadge label="Estimated" />{/if}</span>
											<span class="top-track"><span class="top-fill" style="width:{(s.total_cost_usd / maxTopCost) * 100}%"></span></span>
											<span class="top-val">{formatCost(s.total_cost_usd)}</span>
											<ChevronRight size={14} strokeWidth={1.8} class="top-arrow" />
										</button>
									{/each}
								</div>
							</div>
						</section>
						{/if}
					{/if}
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
 .stats-title { margin: 0; font-size: 24px; font-weight: 600; letter-spacing: -.01em; line-height: 1.25; }
 .stats-section { scroll-margin-top: 24px; margin-top: 40px; }
 .stats-section h3 { font-size: 14px; font-weight: 600; margin: 0 0 20px; }
 .stats-content { padding: 20px; }
 @container settings (max-width: 800px) { .metrics { grid-template-columns: 1fr 1fr; } .metric:nth-child(3) { border-left: 0; } .metric:nth-child(n+3) { border-top: 1px solid var(--st-border); } }
 @media (prefers-reduced-motion: reduce) { .chart-bar, .brk-fill, .prov-fill { transition: none; } }
 /* Meters and bars on this page fill with the teal accent over the shared track. */
 .stats-section { --stats-fill: var(--st-primary); --stats-fill-soft: color-mix(in oklch, var(--st-primary) 70%, transparent); }

 /* An empty section folds to one row: its title and a muted line. */
 .stats-section.is-empty + .stats-section.is-empty { margin-top: 12px; }
 .stats-empty-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 48px; padding: 0 20px; }
 .stats-empty-row h3 { margin: 0; font-size: 14px; font-weight: 600; }
 .stats-empty-row span { color: var(--st-mfg); font-size: 13px; text-align: right; }
 .stats-empty { margin-top: 24px; display: flex; min-height: 280px; }
 .stats-empty :global(.st-empty-detail) { max-width: none; padding-inline: 0; }

	/* Spend-over-time chart */
	.chart { display: flex; flex-direction: column; gap: 8px; }
	/* The readout has its own row above the plot: 16px under it clears the top
	   tick label, which straddles the plot's top edge. */
	.chart-readout { display: flex; align-items: center; gap: 8px; min-height: 20px; margin: 0 0 16px; font-size: 13px; font-variant-numeric: tabular-nums; }
	.chart-readout-day { color: var(--st-mfg); }
	.chart-readout-val { font-weight: 600; color: var(--st-fg); }
	.chart-body { display: grid; grid-template-columns: auto minmax(0, 1fr); column-gap: 10px; row-gap: 8px; }
	.chart-y { position: relative; height: 180px; }
	/* In flow but invisible: sizes the scale column to its widest label. */
	.chart-y .chart-y-sizer { position: static; display: block; height: 0; overflow: hidden; visibility: hidden; transform: none; }
	.chart-y span { position: absolute; right: 0; transform: translateY(50%); font-size: 11px; line-height: 1; color: var(--st-mfg); font-variant-numeric: tabular-nums; white-space: nowrap; }
	.chart-plot { position: relative; height: 180px; border-bottom: 1px solid var(--st-border-strong); }
	.chart-grid i { position: absolute; left: 0; right: 0; height: 0; border-top: 1px solid color-mix(in oklch, var(--st-border) 70%, transparent); }
	.chart-bars { position: absolute; inset: 0; display: grid; grid-template-columns: repeat(var(--days), minmax(0, 1fr)); }
	.chart-col {
		display: flex; align-items: flex-end; justify-content: center; height: 100%; min-width: 0;
		padding: 0 14%; border: 0; border-radius: 0; background: transparent; cursor: default;
		outline: none; -webkit-tap-highlight-color: transparent;
	}
	.chart-bar {
		display: block; width: 100%; max-width: 40px; background: var(--stats-fill-soft);
		border-radius: 3px 3px 0 0; transition: background-color 120ms ease;
	}
	/* Full accent is today's alone. The picked day gets the pale column band and
	   an 85% accent bar, so it never reads as a second "today". */
	.chart-col.today .chart-bar { background: var(--stats-fill); }
	.chart-col.picked:not(.today) .chart-bar { background: color-mix(in oklch, var(--stats-fill) 85%, transparent); }
	.chart-col.picked { background: var(--st-hover); }
	.chart-col:focus-visible .chart-bar { outline: 2px solid var(--ring); outline-offset: 2px; }
	.chart-x { grid-column: 2; position: relative; height: 16px; }
	.chart-x span { position: absolute; top: 0; transform: translateX(-50%); font-size: 11px; line-height: 16px; color: var(--st-mfg); white-space: nowrap; font-variant-numeric: tabular-nums; }
	.chart-x span.first { transform: none; left: 0 !important; }
	.chart-x span.last { transform: none; left: auto !important; right: 0; }
	.chart-x span.today { color: var(--st-fg); font-weight: 500; }
	@container st-detail (max-width: 520px) { .chart-x span.minor { display: none; } }

	/* Cost-by-profile rows: one grid so the tracks line up; a name truncates
	   only when its row is full (the track keeps at least 96px or 30%). */
	.brk { display: grid; grid-template-columns: minmax(0, max-content) minmax(max(96px, 30%), 1fr) auto; column-gap: 12px; row-gap: 11px; margin-top: 2px; }
	.brk-row { display: grid; grid-column: 1 / -1; grid-template-columns: subgrid; align-items: center; }
	.brk-name { display: flex; align-items: center; gap: 8px; min-width: 0; font-size: 13px; color: var(--foreground); }
	.brk-label { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	/* The word "Estimated" wears the same pill as the billing chip beside it. */
	.stats-section :global(.usage-est) { flex-shrink: 0; padding: 1px 7px; border-radius: 999px; border-color: var(--border); font-size: 11px; line-height: 1.4; white-space: nowrap; }
	.brk-track { height: 6px; background: var(--st-meter-track); border-radius: 999px; overflow: hidden; }
	.brk-fill { display: block; height: 100%; background: var(--stats-fill); border-radius: 999px; transition: width 450ms ease; }
	.brk-val { font-size: 13px; font-weight: 600; color: var(--muted-foreground); text-align: right; }

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
	/* The pills wrap under the name rather than squeezing it out of a narrow column. */
	.prov-name { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; min-width: 0; font-size: 13px; color: var(--foreground); }
	.prov-label { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.prov-chip {
		flex-shrink: 0; padding: 1px 7px; border-radius: 999px; font-size: 11px; font-weight: 600;
		border: 1px solid var(--border); color: var(--muted-foreground); white-space: nowrap;
	}
	.prov-chip[data-billing='subscription'] { color: var(--st-mfg); }
	.prov-chip[data-billing='unknown'] { border-style: dashed; }
	.prov-num { font-size: 13px; color: var(--muted-foreground); text-align: right; font-variant-numeric: tabular-nums; }
	.prov-head .prov-num { font-size: 12px; }
	.prov-mlabel { display: none; }
	.prov-track { height: 6px; background: var(--st-meter-track); border-radius: 999px; overflow: hidden; }
	.prov-fill { display: block; height: 100%; background: var(--stats-fill); border-radius: 999px; transition: width 450ms ease; }
	/* Subscription equivalent remains labelled, with a quieter token-coloured track. */
	.prov-fill.sub { background: var(--stats-fill-soft); }
	.prov-cost { display: flex; flex-direction: column; align-items: flex-end; text-align: right; cursor: help; }
	.prov-cost-val { font-size: 13px; font-weight: 600; color: var(--foreground); }
	.prov-cost.missing .prov-cost-val { color: var(--muted-foreground); font-weight: 500; font-style: italic; }
	.prov-cost-note { font-size: 12px; color: var(--st-mfg); white-space: nowrap; }
	.prov-foot { margin: 10px 0 0; font-size: 12px; color: var(--st-mfg); }

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
		color: var(--st-mfg); font-size: 11px; font-weight: 600;
	}
	.top-name { display: flex; align-items: center; gap: 8px; min-width: 0; font-size: 13px; color: var(--foreground); }
	.top-label { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.top-track { height: 5px; background: var(--st-meter-track); border-radius: 999px; overflow: hidden; }
	.top-fill { display: block; height: 100%; background: var(--stats-fill); border-radius: 999px; }
	.top-val { font-size: 13px; font-weight: 600; color: var(--muted-foreground); text-align: right; }
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
