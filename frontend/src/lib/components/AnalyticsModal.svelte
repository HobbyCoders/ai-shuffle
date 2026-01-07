<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import UniversalModal from './UniversalModal.svelte';
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

	import DateRangePicker from '$lib/components/analytics/DateRangePicker.svelte';
	import SummaryCards from '$lib/components/analytics/SummaryCards.svelte';
	import UsageChart from '$lib/components/analytics/UsageChart.svelte';
	import CostBreakdown from '$lib/components/analytics/CostBreakdown.svelte';
	import TopSessionsTable from '$lib/components/analytics/TopSessionsTable.svelte';

	interface Props {
		open: boolean;
		onClose: () => void;
		onSessionClick?: (sessionId: string) => void;
	}

	let { open, onClose, onSessionClick }: Props = $props();

	// Date range state
	let startDate = $state('');
	let endDate = $state('');

	// Data state
	let summary: AnalyticsSummary | null = $state(null);
	let costBreakdown: CostBreakdownItem[] = $state([]);
	let usageTrends: TrendDataPoint[] = $state([]);
	let topSessions: TopSession[] = $state([]);

	// Loading states
	let summaryLoading = $state(true);
	let costBreakdownLoading = $state(true);
	let trendsLoading = $state(true);
	let topSessionsLoading = $state(true);
	let exportLoading = $state(false);

	// Error state
	let error: string | null = $state(null);

	// Cost breakdown grouping
	let costGroupBy: 'profile' | 'user' = $state('profile');

	// Track if we've initialized
	let initialized = $state(false);

	// Initialize dates with 30-day range
	function initializeDates() {
		const end = new Date();
		const start = new Date();
		start.setDate(end.getDate() - 30);

		startDate = formatDate(start);
		endDate = formatDate(end);
	}

	function formatDate(date: Date): string {
		return date.toISOString().split('T')[0];
	}

	// Load all analytics data
	async function loadAnalytics() {
		if (!startDate || !endDate) return;

		error = null;

		// Load all data in parallel
		summaryLoading = true;
		costBreakdownLoading = true;
		trendsLoading = true;
		topSessionsLoading = true;

		try {
			// Summary
			getAnalyticsSummary(startDate, endDate)
				.then(data => {
					summary = data;
					summaryLoading = false;
				})
				.catch(e => {
					console.error('Failed to load summary:', e);
					summary = null;
					summaryLoading = false;
				});

			// Cost breakdown
			getCostBreakdown(startDate, endDate, costGroupBy)
				.then(data => {
					costBreakdown = data;
					costBreakdownLoading = false;
				})
				.catch(e => {
					console.error('Failed to load cost breakdown:', e);
					costBreakdown = [];
					costBreakdownLoading = false;
				});

			// Usage trends
			getUsageTrends(startDate, endDate)
				.then(data => {
					usageTrends = data;
					trendsLoading = false;
				})
				.catch(e => {
					console.error('Failed to load trends:', e);
					usageTrends = [];
					trendsLoading = false;
				});

			// Top sessions
			getTopSessions(startDate, endDate, 10)
				.then(data => {
					topSessions = data;
					topSessionsLoading = false;
				})
				.catch(e => {
					console.error('Failed to load top sessions:', e);
					topSessions = [];
					topSessionsLoading = false;
				});
		} catch (e) {
			console.error('Failed to load analytics:', e);
			error = 'Failed to load analytics data. Please try again.';
		}
	}

	// Handle date range change
	function handleDateChange(event: CustomEvent<{ startDate: string; endDate: string }>) {
		startDate = event.detail.startDate;
		endDate = event.detail.endDate;
		loadAnalytics();
	}

	// Handle cost grouping change
	function handleGroupByChange(newGroupBy: 'profile' | 'user') {
		costGroupBy = newGroupBy;
		costBreakdownLoading = true;
		getCostBreakdown(startDate, endDate, costGroupBy)
			.then(data => {
				costBreakdown = data;
				costBreakdownLoading = false;
			})
			.catch(e => {
				console.error('Failed to load cost breakdown:', e);
				costBreakdown = [];
				costBreakdownLoading = false;
			});
	}

	// Handle export
	async function handleExport() {
		exportLoading = true;
		try {
			await exportAnalyticsCSV(startDate, endDate);
		} catch (e) {
			console.error('Export failed:', e);
			error = 'Failed to export analytics data.';
		} finally {
			exportLoading = false;
		}
	}

	// Navigate to session
	function handleSessionClick(sessionId: string) {
		if (onSessionClick) {
			onSessionClick(sessionId);
		} else {
			goto(`/?session=${sessionId}`);
		}
		onClose();
	}

	// Initialize when modal opens
	$effect(() => {
		if (open && !initialized) {
			initializeDates();
			loadAnalytics();
			initialized = true;
		}
		// Reset when modal closes
		if (!open) {
			initialized = false;
		}
	});
</script>

<UniversalModal
	{open}
	title="Analytics"
	icon="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"
	{onClose}
	showFooter={false}
	size="full"
>
	<div class="space-y-6">
		<!-- Controls Row -->
		<div class="flex flex-wrap items-center justify-between gap-3">
			<p class="text-sm text-muted-foreground">Usage statistics and cost analysis</p>

			<div class="flex flex-wrap items-center gap-3">
				<DateRangePicker
					bind:startDate
					bind:endDate
					on:change={handleDateChange}
				/>

				<button
					onclick={handleExport}
					disabled={exportLoading}
					class="btn btn-secondary flex items-center gap-2 px-4 py-2 rounded-lg bg-muted hover:bg-accent text-foreground border border-border transition-colors disabled:opacity-50"
				>
					{#if exportLoading}
						<div class="animate-spin w-4 h-4 border-2 border-current border-t-transparent rounded-full"></div>
					{:else}
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
						</svg>
					{/if}
					Export CSV
				</button>
			</div>
		</div>

		{#if error}
			<div class="bg-destructive/10 border border-destructive text-destructive px-4 py-3 rounded-xl">
				{error}
			</div>
		{/if}

		<!-- Summary Cards -->
		<SummaryCards {summary} loading={summaryLoading} />

		<!-- Charts row -->
		<div class="grid grid-cols-1 gap-6">
			<!-- Usage Chart -->
			<UsageChart data={usageTrends} loading={trendsLoading} />

			<!-- Cost Breakdown -->
			<CostBreakdown
				data={costBreakdown}
				loading={costBreakdownLoading}
				groupBy={costGroupBy}
				onGroupByChange={handleGroupByChange}
			/>
		</div>

		<!-- Top Sessions Table -->
		<TopSessionsTable
			sessions={topSessions}
			loading={topSessionsLoading}
			onSessionClick={handleSessionClick}
		/>
	</div>
</UniversalModal>
