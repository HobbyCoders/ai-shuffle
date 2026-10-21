/**
 * The Phone side panel (live Android emulator). It shares the one-tool-per-chat
 * side panel with Browser, Terminal and Changes; the emulator itself belongs
 * to the worker, not the chat, so every chat sees the same device.
 *
 * The phone is the agent's tool, so the composer has no toggle for it. The
 * panel opens when the agent uses the Emulator tools live (`autoOpenFor`,
 * called from the session feed) and from the "Show phone" action on an
 * Emulator tool row (`open`). If the user closes it, the agent's later calls
 * leave it closed for that session; only the user reopens it.
 *
 * `capability` decides whether the phone is offered at all: a worker without
 * the emulator API (404) or a viewer it refuses (401/403) never sees it. Any
 * other answer, including "no KVM", offers it so the panel can explain what
 * is missing.
 *
 * The panel always talks to the worker this page is connected to. A chat that
 * runs on another worker drives that worker's emulator, which the panel cannot
 * show yet, so remote chats neither auto-open it nor offer "Show phone".
 */
import { sidePanel, sessionPanelKey } from './side-panel.svelte';
import { fetchEmulatorStatus, errorStatus } from '$lib/api/emulator';

type Capability = 'unknown' | 'loading' | 'available' | 'hidden';
let capability = $state<Capability>('unknown');
let probe: Promise<void> | null = null;
/** Sessions whose phone panel the user closed; agent calls leave them closed. */
let userClosed = $state<Record<string, true>>({});
/** Sessions known to run on another worker than the one this page talks to. */
let remote = $state<Record<string, boolean>>({});

const EMULATOR_TOOLS = new Set(['Emulator', 'EmulatorScreenshot']);

/**
 * True for the app's Emulator tools however the leg names them: bare
 * (`Emulator`), MCP-bridged (`mcp__builtins__Emulator`) or wrapped by the
 * delegated CLI's bundled server (`mcp__fluid_bundled__mcp__builtins__Emulator`).
 * A third-party MCP server's own "Emulator" tool (`mcp__other__Emulator`) is not ours.
 */
export function isEmulatorTool(name: unknown): boolean {
	if (typeof name !== 'string') return false;
	const bare = name.replace(/^mcp__fluid_bundled__/, '').replace(/^mcp__builtins__/, '');
	return EMULATOR_TOOLS.has(bare);
}

export const phonePanel = {
	get capability(): Capability { return capability; },
	/** Probe once per app load (cheap GET); safe to call from effects. */
	ensureCapability(): Promise<void> {
		if (probe) return probe;
		capability = 'loading';
		probe = fetchEmulatorStatus().then(
			() => { capability = 'available'; },
			(error) => {
				const status = errorStatus(error);
				// Older workers and refused viewers: no phone. A network blip or a
				// 5xx keeps it, so the panel can say what is wrong.
				capability = status === 404 || status === 401 || status === 403 || status === 405 ? 'hidden' : 'available';
			},
		);
		return probe;
	},
	/** Record whether a session runs on another worker (fed by the main panel). */
	noteRemote(sessionId: string | null, isRemote: boolean) {
		if (sessionId && (remote[sessionId] ?? false) !== isRemote) remote[sessionId] = isRemote;
	},
	/** Whether a session may show this worker's phone ("Show phone" on a tool row). */
	offeredFor(sessionId: string | null | undefined): boolean {
		return !!sessionId && capability === 'available' && !remote[sessionId];
	},
	isOpen(sessionId: string | null): boolean {
		return !!sessionId && sidePanel.forScope(sessionPanelKey(sessionId)).active === 'phone';
	},
	/** The user asked for the phone: open it and let the agent's calls open it again. */
	open(sessionId: string | null) {
		if (!sessionId) return;
		delete userClosed[sessionId];
		sidePanel.open(sessionPanelKey(sessionId), 'phone');
	},
	/** The user closed the phone: the agent's later calls in this session leave it closed. */
	close(sessionId: string | null) {
		if (!sessionId || !this.isOpen(sessionId)) return;
		userClosed[sessionId] = true;
		sidePanel.close(sessionPanelKey(sessionId), 'phone');
	},
	userClosedFor(sessionId: string | null): boolean {
		return !!sessionId && !!userClosed[sessionId];
	},
	/**
	 * The agent used an Emulator tool (a LIVE event; history replay never calls
	 * this). Opens the phone unless the user closed it for this session, the
	 * chat runs on another worker, or the user is looking at the terminal or
	 * the browser: the phone takes an empty panel or the Changes list, as the
	 * browser does.
	 */
	autoOpenFor(sessionId: string | null, opts: { remote?: boolean } = {}): Promise<void> {
		if (!sessionId || opts.remote) return Promise.resolve();
		const openNow = () => {
			if (capability !== 'available' || userClosed[sessionId] || remote[sessionId]) return;
			const key = sessionPanelKey(sessionId);
			const active = sidePanel.forScope(key).active;
			if (active === null || active === 'changes') sidePanel.open(key, 'phone');
		};
		// Usually already probed (the main panel asks on opening a chat): open
		// in the same tick as the event. Otherwise ask first.
		if (capability === 'available' || capability === 'hidden') {
			openNow();
			return Promise.resolve();
		}
		return this.ensureCapability().then(openNow);
	},
	/** Test hook. */
	reset() {
		capability = 'unknown';
		probe = null;
		userClosed = {};
		remote = {};
	},
};
