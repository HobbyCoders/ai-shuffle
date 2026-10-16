/**
 * The Phone side panel (live Android emulator). It shares the one-tool-per-chat
 * side panel with Browser, Terminal and Changes; the emulator itself belongs
 * to the worker, not the chat, so every chat sees the same device.
 *
 * `capability` decides whether the composer offers the toggle at all: a
 * worker without the emulator API (404) or a viewer it refuses (401/403)
 * never sees it. Any other answer, including "no KVM", shows the toggle so
 * the panel can explain what is missing.
 */
import { sidePanel, sessionPanelKey } from './side-panel.svelte';
import { fetchEmulatorStatus, errorStatus } from '$lib/api/emulator';

type Capability = 'unknown' | 'loading' | 'available' | 'hidden';
let capability = $state<Capability>('unknown');

export const phonePanel = {
	get capability(): Capability { return capability; },
	/** Probe once per app load (cheap GET); safe to call from effects. */
	async ensureCapability(): Promise<void> {
		if (capability !== 'unknown') return;
		capability = 'loading';
		try {
			await fetchEmulatorStatus();
			capability = 'available';
		} catch (error) {
			const status = errorStatus(error);
			// Older workers and refused viewers: no toggle. A network blip or a
			// 5xx keeps it, so the panel can say what is wrong.
			capability = status === 404 || status === 401 || status === 403 || status === 405 ? 'hidden' : 'available';
		}
	},
	isOpen(sessionId: string | null): boolean {
		return !!sessionId && sidePanel.forScope(sessionPanelKey(sessionId)).active === 'phone';
	},
	open(sessionId: string | null) {
		if (sessionId) sidePanel.open(sessionPanelKey(sessionId), 'phone');
	},
	close(sessionId: string | null) {
		if (sessionId) sidePanel.close(sessionPanelKey(sessionId), 'phone');
	},
	toggle(sessionId: string | null) {
		if (this.isOpen(sessionId)) this.close(sessionId);
		else this.open(sessionId);
	},
	/** Test hook. */
	reset() { capability = 'unknown'; },
};
