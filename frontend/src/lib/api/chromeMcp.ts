/**
 * Claude in Chrome (browser automation) API client.
 *
 * Talks to app/api/chrome_mcp.py: status, enable/disable, CDP targets, and the
 * WebSocket URL for the live "Set up browser" screencast panel.
 */

import { api } from './client';

export interface ChromeMcpStatus {
	enabled: boolean;
	/**
	 * 'container' = supervised headless Chrome + in-app CDP setup panel;
	 * 'native' = the user's own desktop Chrome (no CDP; sign in via Chrome itself).
	 */
	mode: 'container' | 'native';
	chrome_running: boolean;
	cdp_available: boolean;
	cdp_enabled: boolean;
	extension_installed: boolean;
	native_manifest_present: boolean;
	native_host_present: boolean;
	bridge_active: boolean;
	/** A specific device is pinned in ~/.claude.json (pairedDeviceId). */
	paired: boolean;
	paired_device_id: string | null;
	paired_device_name: string | null;
	/** Signed in & talking to the native host — working even if not pinned. */
	connected: boolean;
	ready: boolean;
	next_step: string;
	cdp_port: number;
	extension_options_url: string | null;
}

/** Result of pinning this container's own browser (POST /chrome-mcp/pair). */
export interface PairResult {
	ok: boolean;
	message: string;
	paired_device_id: string | null;
	paired_device_name: string | null;
	status: ChromeMcpStatus;
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

/**
 * Pin this container's own browser as the persisted pairedDeviceId so the agent
 * deterministically uses it even when another browser is on the same account.
 */
export async function pairChromeMcp(): Promise<PairResult> {
	return api.post<PairResult>('/chrome-mcp/pair', {});
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
