/**
 * Browser automation API client.
 *
 * Talks to app/api/browser.py: report status of the built-in `browser` MCP
 * server (whether it's enabled and whether the supervised Chromium is reachable
 * over loopback CDP) and enable/disable it. No account, pairing, or setup flow —
 * the server is login-less and spawned per session.
 */

import { api } from './client';

export interface BrowserStatus {
	/** The `browser` built-in is enabled (not in disabledMcpServers). */
	enabled: boolean;
	/** The supervised Chromium answers on the loopback CDP port. */
	browser_running: boolean;
	/** Enabled AND the browser is reachable — the tools can actually drive it. */
	tool_server_ok: boolean;
}

export async function getBrowserStatus(): Promise<BrowserStatus> {
	return api.get<BrowserStatus>('/browser/status');
}

export async function toggleBrowser(enabled: boolean): Promise<BrowserStatus> {
	return api.post<BrowserStatus>('/browser/toggle', { enabled });
}
