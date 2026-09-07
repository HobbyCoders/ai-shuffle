/**
 * Reachability probes against a worker's public `/health` endpoint.
 *
 * Runs in the main process with Chromium's network stack so it sees the same
 * proxies and certificate decisions as the window itself.
 */
import { net } from 'electron';

export interface ProbeResult {
	ok: boolean;
	/** Server version string when the health payload carried one. */
	version: string | null;
	/** True when the health payload says first-run setup is still needed. */
	setupRequired: boolean;
	error: string | null;
}

const LOCAL_CANDIDATES = ['http://localhost:8000', 'http://127.0.0.1:8000'];

export async function probe(origin: string, timeoutMs = 4000): Promise<ProbeResult> {
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), timeoutMs);
	try {
		const response = await net.fetch(`${origin}/health`, {
			signal: controller.signal,
			cache: 'no-store',
			credentials: 'omit'
		});
		if (!response.ok) {
			return { ok: false, version: null, setupRequired: false, error: `HTTP ${response.status}` };
		}
		const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
		const service = typeof body.service === 'string' ? body.service : '';
		if (service && service !== 'ai-shuffle') {
			return { ok: false, version: null, setupRequired: false, error: 'That address is not an AI Shuffle worker.' };
		}
		return {
			ok: true,
			version: typeof body.version === 'string' ? body.version : null,
			setupRequired: body.setup_required === true,
			error: null
		};
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		return {
			ok: false,
			version: null,
			setupRequired: false,
			error: controller.signal.aborted ? 'No answer within a few seconds.' : message
		};
	} finally {
		clearTimeout(timer);
	}
}

/** Finds a worker running on this computer at the default port, if any. */
export async function detectLocal(): Promise<{ origin: string; result: ProbeResult } | null> {
	for (const origin of LOCAL_CANDIDATES) {
		const result = await probe(origin, 1500);
		if (result.ok) return { origin, result };
	}
	return null;
}
