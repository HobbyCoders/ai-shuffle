<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { auth, isAuthenticated, isAdmin } from '$lib/stores/auth';
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

	// Date range state
	let startDate = '';
	let endDate = '';

	// Data state
	let summary: AnalyticsSummary | null = null;
	let costBreakdown: CostBreakdownItem[] = [];
	let usageTrends: TrendDataPoint[] = [];
	let topSessions: TopSession[] = [];

	// Loading states
	let summaryLoading = true;
	let costBreakdownLoading = true;
	let trendsLoading = true;
	let topSessionsLoading = true;
	let exportLoading = false;

	// Error state
	let error: string | null = null;

	// Cost breakdown grouping
	let costGroupBy: 'profile' | 'user' = 'profile';

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
		goto(`/?session=${sessionId}`);
	}

	// Check auth on mount
	onMount(async () => {
		await auth.checkAuth();

		if (!$isAuthenticated) {
			goto('/login');
			return;
		}

		if (!$isAdmin) {
			goto('/');
			return;
		}

		initializeDates();
		loadAnalytics();
	});
</script>

<svelte:head>
	<title>Analytics - AI Hub</title>
</svelte:head>

<div class="min-h-screen bg-background text-foreground">
	<!-- Header -->
	<header class="sticky top-0 z-10 bg-background/80 backdrop-blur-lg border-b border-border">
		<div class="max-w-7xl mx-auto px-4 py-4">
			<div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
				<div class="flex items-center gap-4">
					<a href="/" class="p-2 -ml-2 rounded-lg hover:bg-muted transition-colors" title="Back to Chat">
						<svg class="w-5 h-5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18" />
						</svg>
					</a>
					<div>
						<h1 class="text-xl font-bold text-foreground">Analytics</h1>
						<p class="text-sm text-muted-foreground">Usage statistics and cost analysis</p>
					</div>
				</div>

				<div class="flex flex-wrap items-center gap-3">
					<DateRangePicker
						bind:startDate
						bind:endDate
						on:change={handleDateChange}
					/>

					<button
						on:click={handleExport}
						disabled={exportLoading}
						class="btn btn-secondary flex items-center gap-2"
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
		</div>
	</header>

	<!-- Main content -->
	<main class="max-w-7xl mx-auto px-4 py-6 space-y-6">
		{#if error}
			<div class="bg-destructive/10 border border-destructive text-destructive px-4 py-3 rounded-xl">
				{error}
			</div>
		{/if}

		<!-- Summary Cards -->
		<SummaryCards {summary} loading={summaryLoading} />

		<!-- Charts row -->
		<div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
			<!-- Usage Chart -->
			<div class="lg:col-span-2">
				<UsageChart data={usageTrends} loading={trendsLoading} />
			</div>

			<!-- Cost Breakdown -->
			<div class="lg:col-span-2">
				<CostBreakdown
					data={costBreakdown}
					loading={costBreakdownLoading}
					groupBy={costGroupBy}
					onGroupByChange={handleGroupByChange}
				/>
			</div>
		</div>

		<!-- Top Sessions Table -->
		<TopSessionsTable
			sessions={topSessions}
			loading={topSessionsLoading}
			onSessionClick={handleSessionClick}
		/>
	</main>
</div>
