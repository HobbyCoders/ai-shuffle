/**
 * Analytics API types and functions
 */

import { api, API_BASE, type ApiError } from './client-core';

// ============================================================================
// Public Types (exported via barrel)
// ============================================================================

export interface AnalyticsSummary {
	total_input_tokens: number;
	total_output_tokens: number;
	total_cost: number;
	session_count: number;
	average_cost_per_session: number;
}

export interface CostBreakdownItem {
	name: string;
	cost: number;
	tokens: number;
	percentage: number;
}

export interface TrendDataPoint {
	date: string;
	input_tokens: number;
	output_tokens: number;
	cost: number;
}

export interface TopSession {
	id: string;
	title: string | null;
	profile_id: string | null;
	total_cost_usd: number;
	total_tokens_in: number;
	total_tokens_out: number;
	created_at: string;
}

// ============================================================================
// Private Types (backend response shapes, not exported)
// ============================================================================

interface BackendUsageStats {
	total_tokens_in: number;
	total_tokens_out: number;
	total_cost_usd: number;
	session_count: number;
	query_count: number;
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
}

// ============================================================================
// Functions
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
		average_cost_per_session: sessionCount > 0 ? totalCost / sessionCount : 0
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
		percentage: totalCost > 0 ? (item.total_cost_usd / totalCost) * 100 : 0
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
		cost: point.cost_usd
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
		created_at: session.created_at || new Date().toISOString()
	}));
}

/**
 * Export analytics data as CSV
 */
export async function exportAnalyticsCSV(startDate: string, endDate: string): Promise<void> {
	const params = new URLSearchParams({ start_date: startDate, end_date: endDate });
	const response = await fetch(`${API_BASE}/analytics/export?${params.toString()}`, {
		method: 'GET',
		credentials: 'include'
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: 'Export failed' }));
		throw {
			detail: error.detail || 'Export failed',
			status: response.status
		} as ApiError;
	}

	// Get filename from Content-Disposition header
	const contentDisposition = response.headers.get('Content-Disposition');
	let filename = `analytics-${startDate}-to-${endDate}.csv`;
	if (contentDisposition) {
		const match = contentDisposition.match(/filename="?([^"]+)"?/);
		if (match) {
			filename = match[1];
		}
	}

	// Create blob and trigger download
	const blob = await response.blob();
	const url = URL.createObjectURL(blob);
	const a = document.createElement('a');
	a.href = url;
	a.download = filename;
	document.body.appendChild(a);
	a.click();
	document.body.removeChild(a);
	URL.revokeObjectURL(url);
}
