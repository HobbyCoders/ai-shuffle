/**
 * Claude in Chrome (browser automation) API client.
 *
 * Talks to app/api/chrome_mcp.py: status, enable/disable, CDP targets, and the
 * WebSocket URL for the live "Set up browser" screencast panel.
 */

import { api } from './client';

export interface ChromeMcpStatus {
	enabled: boolean;
	chrome_running: boolean;
	cdp_available: boolean;
	cdp_enabled: boolean;
	extension_installed: boolean;
	native_manifest_present: boolean;
	native_host_present: boolean;
	bridge_active: boolean;
	paired: boolean;
	paired_device_name: string | null;
	ready: boolean;
	next_step: string;
	cdp_port: number;
	extension_options_url: string | null;
}

export interface CdpTarget {
	id: string;
	type: string;
	title: string;
	url: string;
}

export async function getChromeMcpStatus(): Promise<ChromeMcpStatus> {
	return api.get<ChromeMcpStatus>('/chrome-mcp/status');
}

export async function toggleChromeMcp(enabled: boolean): Promise<ChromeMcpStatus> {
	return api.post<ChromeMcpStatus>('/chrome-mcp/toggle', { enabled });
}

export async function getChromeTargets(): Promise<CdpTarget[]> {
	return api.get<CdpTarget[]>('/chrome-mcp/targets');
}

/**
 * WebSocket URL for the CDP proxy. Auth rides on the same-origin session cookie
 * (httpOnly), so no token is needed in the query string.
 */
export function chromeCdpWsUrl(targetId?: string): string {
	const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
	const base = `${proto}//${window.location.host}/api/v1/chrome-mcp/cdp`;
	return targetId ? `${base}?target=${encodeURIComponent(targetId)}` : base;
}
