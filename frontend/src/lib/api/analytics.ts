/**
 * Analytics API types and functions
 */

import { api, fetchAndDownload } from './client';

// ============================================================================
// Frontend-friendly Types (transformed from backend responses)
// ============================================================================

export interface AnalyticsSummary {
	total_input_tokens: number;
	total_output_tokens: number;
	total_cost: number;
	session_count: number;
	average_cost_per_session: number;
	/** Turns in range whose token counts the harness estimated (the provider
	 *  reported no usage, or the reply was interrupted). Their amounts are
	 *  inside every total above; non-zero means the totals are partly
	 *  estimates and the panel shows the "est." marker. */
	estimated_query_count: number;
}

export interface CostBreakdownItem {
	name: string;
	cost: number;
	tokens: number;
	percentage: number;
	/** True when any turn behind this row has estimated token counts. */
	estimated: boolean;
}

export interface TrendDataPoint {
	date: string;
	input_tokens: number;
	output_tokens: number;
	cost: number;
	/** True when any turn in this bucket has estimated token counts. */
	estimated: boolean;
}

export interface TopSession {
	id: string;
	title: string | null;
	profile_id: string | null;
	total_cost_usd: number;
	total_tokens_in: number;
	total_tokens_out: number;
	created_at: string;
	/** True when any booked turn of the session has estimated token counts. */
	usage_estimated: boolean;
}

// ============================================================================
// Backend response types (for transformation)
// ============================================================================

interface BackendUsageStats {
	total_tokens_in: number;
	total_tokens_out: number;
	total_cost_usd: number;
	session_count: number;
	query_count: number;
	estimated_query_count?: number;
}

interface BackendAnalyticsSummaryResponse {
	start_date: string;
	end_date: string;
	usage_stats: BackendUsageStats;
	top_profiles: BackendCostBreakdownItem[];
	recent_trend: BackendUsageTrend[];
}

interface BackendCostBreakdownItem {
	key: string;
	name: string | null;
	total_cost_usd: number;
	total_tokens_in: number;
	total_tokens_out: number;
	query_count: number;
	estimated_query_count?: number;
}

interface BackendCostBreakdownResponse {
	group_by: string;
	start_date: string;
	end_date: string;
	items: BackendCostBreakdownItem[];
	total_cost_usd: number;
}

interface BackendUsageTrend {
	date: string;
	tokens_in: number;
	tokens_out: number;
	cost_usd: number;
	query_count: number;
	estimated_query_count?: number;
}

interface BackendUsageTrendsResponse {
	interval: string;
	start_date: string;
	end_date: string;
	data_points: BackendUsageTrend[];
}

interface BackendTopSession {
	session_id: string;
	title: string | null;
	profile_id: string | null;
	profile_name: string | null;
	total_cost_usd: number;
	total_tokens_in: number;
	total_tokens_out: number;
	created_at: string | null;
	usage_estimated?: boolean;
}

// ============================================================================
// Analytics API Functions
// ============================================================================

/**
 * Get analytics summary for a date range
 * Transforms backend response to frontend-friendly format
 */
export async function getAnalyticsSummary(startDate: string, endDate: string): Promise<AnalyticsSummary> {
	const params = new URLSearchParams({ start_date: startDate, end_date: endDate });
	const response = await api.get<BackendAnalyticsSummaryResponse>(`/analytics/summary?${params.toString()}`);

	const stats = response.usage_stats;
	const totalCost = stats.total_cost_usd;
	const sessionCount = stats.session_count;

	return {
		total_input_tokens: stats.total_tokens_in,
		total_output_tokens: stats.total_tokens_out,
		total_cost: totalCost,
		session_count: sessionCount,
		average_cost_per_session: sessionCount > 0 ? totalCost / sessionCount : 0,
		estimated_query_count: stats.estimated_query_count ?? 0
	};
}

/**
 * Get cost breakdown grouped by profile or user
 * Transforms backend response to frontend-friendly format with percentages
 */
export async function getCostBreakdown(startDate: string, endDate: string, groupBy: 'profile' | 'user' = 'profile'): Promise<CostBreakdownItem[]> {
	const params = new URLSearchParams({ start_date: startDate, end_date: endDate, group_by: groupBy });
	const response = await api.get<BackendCostBreakdownResponse>(`/analytics/costs?${params.toString()}`);

	const totalCost = response.total_cost_usd || response.items.reduce((sum, item) => sum + item.total_cost_usd, 0);

	return response.items.map(item => ({
		name: item.name || 'Unknown',
		cost: item.total_cost_usd,
		tokens: item.total_tokens_in + item.total_tokens_out,
		percentage: totalCost > 0 ? (item.total_cost_usd / totalCost) * 100 : 0,
		estimated: (item.estimated_query_count ?? 0) > 0
	}));
}

/**
 * Get usage trends over time
 * Transforms backend response to frontend-friendly format
 */
export async function getUsageTrends(startDate: string, endDate: string): Promise<TrendDataPoint[]> {
	const params = new URLSearchParams({ start_date: startDate, end_date: endDate });
	const response = await api.get<BackendUsageTrendsResponse>(`/analytics/trends?${params.toString()}`);

	return response.data_points.map(point => ({
		date: point.date,
		input_tokens: point.tokens_in,
		output_tokens: point.tokens_out,
		cost: point.cost_usd,
		estimated: (point.estimated_query_count ?? 0) > 0
	}));
}

/**
 * Get top sessions by cost
 * Transforms backend response to frontend-friendly format
 */
export async function getTopSessions(startDate: string, endDate: string, limit: number = 10): Promise<TopSession[]> {
	const params = new URLSearchParams({ start_date: startDate, end_date: endDate, limit: limit.toString() });
	const response = await api.get<BackendTopSession[]>(`/analytics/top-sessions?${params.toString()}`);

	return response.map(session => ({
		id: session.session_id,
		title: session.title,
		profile_id: session.profile_id,
		total_cost_usd: session.total_cost_usd,
		total_tokens_in: session.total_tokens_in,
		total_tokens_out: session.total_tokens_out,
		created_at: session.created_at || new Date().toISOString(),
		usage_estimated: session.usage_estimated === true
	}));
}

/**
 * Export analytics data as CSV
 */
export async function exportAnalyticsCSV(startDate: string, endDate: string): Promise<void> {
	const params = new URLSearchParams({ start_date: startDate, end_date: endDate });
	await fetchAndDownload(`/analytics/export?${params.toString()}`, `analytics-${startDate}-to-${endDate}.csv`);
}

