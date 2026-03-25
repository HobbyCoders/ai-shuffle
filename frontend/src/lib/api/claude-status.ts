/**
 * Claude service status API
 */

import { api } from './client';

export interface ComponentStatus {
	name: string;
	status: 'operational' | 'degraded_performance' | 'partial_outage' | 'major_outage';
}

export interface Incident {
	name: string;
	status: string;
	impact: 'none' | 'minor' | 'major' | 'critical';
	shortlink: string;
	started_at: string;
	latest_update: string | null;
}

export interface ClaudeStatusResponse {
	ok: boolean;
	indicator: 'none' | 'minor' | 'major' | 'critical';
	description: string;
	components: ComponentStatus[];
	incidents: Incident[];
	fetched_at: string;
}

export async function getClaudeStatus(): Promise<ClaudeStatusResponse> {
	return api.get<ClaudeStatusResponse>('/claude-status');
}
