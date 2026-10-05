/**
 * The setup wizard's first agent run: one short prompt in a new chat session,
 * with the reply streamed back so the owner sees an agent answer.
 *
 * It uses the same paths as the app's composer: the turn is started over the
 * chat WebSocket (`/api/v1/ws/chat`, a `query` frame with `mode: 'chat'`, so
 * the session is a chat session with no project checkout and no file tools),
 * and the reply is read from the session's event feed
 * (`/api/v1/sessions/{id}/events`). The run goes through the worker's normal
 * turn start, so the credential and license gates apply to it exactly as to
 * any other run, and whatever they refuse comes back as the error.
 *
 * The run names its model explicitly (`overrides.model`), picked from a
 * provider that is ready (see `pickFirstRunModel`), so a worker whose default
 * model belongs to a provider without a credential still answers.
 *
 * Two refusals carry a code on the chat WebSocket's `error` frame
 * (`{ type: 'error', session_id, message, code }`):
 * - `agent_setup_required`: the credential gate refused the turn (no license,
 *   or no usable credential for the provider the run needs). `message` says
 *   what is missing and where to fix it.
 * - `loop_guard_confirm`: the subscription loop guard held an unattended turn
 *   for the owner's confirmation. `message` says why and where to confirm it.
 * The session's event feed reports the same refusal as `turn.aborted` without
 * the code, often first, so an abort waits briefly for the coded frame.
 */
import { api, getErrorMessage, type Profile } from '$lib/api/client';
import type { ModelProvider } from '$lib/api/model-providers';
import { wsUrl } from '$lib/api/runtime';
import { providerOfRow } from '$lib/components/settings/providers/providers-model';
import {
	decodeProviderModel, encodeProviderModel, type ModelOption, type ProviderEntry
} from '$lib/constants/models.svelte';
import { createSseFeed, type SseFeed } from '$lib/stores/session/v2/sse-feed';

export const FIRST_RUN_PROMPT = 'Say hello in one short sentence, then list three things you can help me with, one line each.';

/** How long to wait for the reply before giving up. */
const FIRST_RUN_TIMEOUT_MS = 180_000;

/** How long an abort from the feed waits for the WebSocket's coded error frame. */
export const ABORT_GRACE_MS = 1000;

/** The credential gate refused the turn: setup is not finished. */
export const SETUP_REQUIRED_CODE = 'agent_setup_required';
/** The subscription loop guard held the turn for the owner's confirmation. */
export const LOOP_GUARD_CODE = 'loop_guard_confirm';
export type FirstRunErrorCode = typeof SETUP_REQUIRED_CODE | typeof LOOP_GUARD_CODE;

function errorCode(value: unknown): FirstRunErrorCode | null {
	return value === SETUP_REQUIRED_CODE || value === LOOP_GUARD_CODE ? value : null;
}

export type FirstRunPhase = 'starting' | 'waiting' | 'streaming' | 'done' | 'failed' | 'needs-input';

export interface FirstRunState {
	phase: FirstRunPhase;
	/** The reply so far. */
	text: string;
	sessionId: string | null;
	/** Why the run failed or stopped, fit to show the user. */
	error: string | null;
	/** The worker's code for a refused or held turn, when it gave one. */
	code: FirstRunErrorCode | null;
}

export interface FirstRunHandle {
	/** Settles with the final state; never rejects. */
	done: Promise<FirstRunState>;
	/** Stop listening. The worker finishes or stops the turn on its own. */
	cancel: () => void;
}

const STARTER_AGENT = {
	name: 'Assistant',
	description: 'Created by setup for your first run. Rename, change or delete it any time.',
	config: {},
};

/**
 * The agent the first run uses: the first agent the owner made, or a new
 * "Assistant". The built-in agents are seeded on every start and list first,
 * so they are skipped.
 */
export async function ensureStarterAgent(): Promise<Profile> {
	const agents = await api.get<Profile[]>('/agents');
	const own = Array.isArray(agents) ? agents.find((agent) => !agent.is_builtin) : undefined;
	if (own) return own;
	return api.post<Profile>('/agents', STARTER_AGENT);
}

function randomId(): string {
	try {
		if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
	} catch {
		// Fall through to the non-cryptographic id; it only matches our own frame.
	}
	return `setup-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function str(value: unknown): string {
	return typeof value === 'string' ? value : '';
}

// ---------------------------------------------------------------------------
// Which provider and model the first run uses
// ---------------------------------------------------------------------------

export interface FirstRunModelInput {
	/** Ready agent providers: `claude`, `openai`, `grok`, `openrouter`, `gw:<id>`. */
	ready: readonly string[];
	/** The provider the owner connected in this wizard, tried first. */
	preferred?: string | null;
	/** model_providers rows (the providers store), to map brands onto rows. */
	rows: readonly ModelProvider[];
	/** Enabled provider rows with their curated models (the model registry). */
	registry: readonly ProviderEntry[];
	/** The worker's default model (a Claude alias or a `prov:` composite). */
	defaultModel: string;
	/** The Claude models the registry offers. */
	claudeModels: readonly ModelOption[];
}

export interface FirstRunModel {
	/** Brand id: `claude`, `openai`, `grok`, `openrouter`, `gw:<id>`. */
	providerId: string;
	/** The value sent as `overrides.model`; null leaves the worker's default. */
	model: string | null;
	/** "OpenAI", "xAI Grok", or the gateway's name. */
	providerName: string;
	/** The model's label, when known. */
	modelLabel: string | null;
}

const BRAND_NAMES: Record<string, string> = { claude: 'Claude', openai: 'OpenAI', grok: 'xAI Grok', openrouter: 'OpenRouter' };

/** The registry rows of a brand, subscriptions first (they are what the owner pays for already). */
function brandEntries(brand: string, input: FirstRunModelInput): { entry: ProviderEntry; row: ModelProvider | undefined }[] {
	return input.registry
		.map((entry) => ({ entry, row: input.rows.find((r) => r.id === entry.id) }))
		.filter(({ entry, row }) => (row ? providerOfRow(row) === brand : `gw:${entry.id}` === brand))
		.sort((a, b) => Number(b.entry.auth_kind === 'subscription') - Number(a.entry.auth_kind === 'subscription'));
}

function forBrand(brand: string, input: FirstRunModelInput): FirstRunModel | null {
	if (brand === 'claude') {
		const isClaudeDefault = !decodeProviderModel(input.defaultModel) && !!input.defaultModel;
		const option = isClaudeDefault
			? input.claudeModels.find((m) => m.value === input.defaultModel)
			: input.claudeModels[0];
		const model = isClaudeDefault ? input.defaultModel : (option?.value ?? null);
		return { providerId: 'claude', model, providerName: 'Claude', modelLabel: option?.label ?? null };
	}
	const decoded = decodeProviderModel(input.defaultModel);
	for (const { entry, row } of brandEntries(brand, input)) {
		// The worker's own default, when it already points at this brand.
		const modelId = decoded?.providerId === entry.id
			? decoded.modelId
			: entry.default_model || entry.models[0]?.id || null;
		if (!modelId) continue;
		const label = entry.models.find((m) => m.id === modelId)?.label ?? modelId;
		const name = BRAND_NAMES[brand] ?? row?.name ?? entry.name;
		return { providerId: brand, model: encodeProviderModel(entry.id, modelId), providerName: name, modelLabel: label };
	}
	return null;
}

/**
 * The provider and model for the first run: the provider connected in this
 * wizard when it is ready, then the worker's default when its provider is
 * ready, then the first ready provider with a model. Null when nothing ready
 * has a model to name; the run then goes out with the worker's default.
 */
export function pickFirstRunModel(input: FirstRunModelInput): FirstRunModel | null {
	const ready = new Set(input.ready);
	const order: string[] = [];
	if (input.preferred && ready.has(input.preferred)) order.push(input.preferred);
	const decoded = decodeProviderModel(input.defaultModel);
	if (decoded) {
		const row = input.rows.find((r) => r.id === decoded.providerId);
		const brand = row ? providerOfRow(row) : `gw:${decoded.providerId}`;
		if (ready.has(brand)) order.push(brand);
	} else if (ready.has('claude')) {
		order.push('claude');
	}
	order.push(...input.ready);
	for (const brand of new Set(order)) {
		const pick = forBrand(brand, input);
		if (pick) return pick;
	}
	return null;
}

interface StartOptions {
	profileId: string;
	prompt?: string;
	/** Model to run with (`overrides.model`); omitted uses the worker's default. */
	model?: string | null;
	onUpdate?: (state: FirstRunState) => void;
	timeoutMs?: number;
	/** Test seams. */
	openSocket?: (url: string) => WebSocket;
	openFeed?: (sessionId: string, onEvent: (event: Record<string, unknown>) => void) => Pick<SseFeed, 'stop'>;
}

export function startFirstAgentRun(options: StartOptions): FirstRunHandle {
	const prompt = options.prompt ?? FIRST_RUN_PROMPT;
	const requestId = randomId();
	const state: FirstRunState = { phase: 'starting', text: '', sessionId: null, error: null, code: null };
	let socket: WebSocket | null = null;
	let feed: Pick<SseFeed, 'stop'> | null = null;
	let settled = false;
	let sawDelta = false;
	let timeout: ReturnType<typeof setTimeout> | undefined;
	let doneGrace: ReturnType<typeof setTimeout> | undefined;
	let abortGrace: ReturnType<typeof setTimeout> | undefined;
	let resolveDone!: (value: FirstRunState) => void;
	const done = new Promise<FirstRunState>((resolve) => { resolveDone = resolve; });

	const publish = () => options.onUpdate?.({ ...state });

	function teardown() {
		clearTimeout(timeout);
		clearTimeout(doneGrace);
		clearTimeout(abortGrace);
		try { feed?.stop(); } catch { /* already stopped */ }
		feed = null;
		if (socket) {
			const s = socket;
			socket = null;
			s.onmessage = null; s.onclose = null; s.onerror = null;
			try { s.close(1000, 'Setup run finished'); } catch { /* already closed */ }
		}
	}

	function finish(phase: 'done' | 'failed' | 'needs-input', error: string | null = null, code: FirstRunErrorCode | null = null) {
		if (settled) return;
		settled = true;
		state.phase = phase;
		state.error = error;
		state.code = code;
		if (phase === 'done' && !state.text.trim()) {
			state.phase = 'failed';
			state.error = 'The agent finished without a reply. Open the session from the sidebar to see what happened.';
		}
		teardown();
		publish();
		resolveDone({ ...state });
	}

	function onFeedEvent(event: Record<string, unknown>) {
		if (settled) return;
		switch (event.type) {
			case 'assistant.text_delta': {
				sawDelta = true;
				state.text += str(event.text);
				state.phase = 'streaming';
				publish();
				break;
			}
			case 'message.assistant': {
				// A closed reply with no deltas (or the full text after them).
				if (str(event.kind) === 'thinking') break;
				const content = str(event.content);
				if (content && (!sawDelta || content.length > state.text.length)) {
					state.text = content;
					state.phase = 'streaming';
					publish();
				}
				break;
			}
			case 'turn.completed':
				finish('done');
				break;
			case 'turn.aborted': {
				// A refused turn is reported here without its code; the
				// WebSocket's error frame carries it and settles the run first
				// when it arrives within the grace.
				const message = str(event.message) || 'The run stopped before the agent answered.';
				clearTimeout(abortGrace);
				abortGrace = setTimeout(() => finish('failed', message), ABORT_GRACE_MS);
				break;
			}
			case 'error':
				finish('failed', str(event.message) || 'The agent could not start.');
				break;
			case 'interaction.requested':
				finish('needs-input', 'The agent is asking for a decision. Open the session from the sidebar to answer it.');
				break;
		}
	}

	function onSocketMessage(raw: MessageEvent) {
		let frame: Record<string, unknown>;
		try { frame = JSON.parse(String(raw.data)); } catch { return; }
		const type = str(frame.type);
		if (type === 'ping') {
			try { socket?.send(JSON.stringify({ type: 'pong' })); } catch { /* closing */ }
			return;
		}
		if (type === 'session_created' && str(frame.request_id) === requestId && !state.sessionId) {
			state.sessionId = str(frame.session_id);
			state.phase = 'waiting';
			publish();
			const open = options.openFeed ?? ((id, onEvent) => createSseFeed(id, { onEvent }));
			feed = open(state.sessionId, onFeedEvent);
			return;
		}
		// Frames for other sessions share this socket; only ours matter.
		const frameSession = str(frame.session_id);
		if (frameSession && state.sessionId && frameSession !== state.sessionId) return;
		if (type === 'error') {
			finish('failed', str(frame.message) || 'The agent could not start.', errorCode(frame.code));
		} else if (type === 'permission_request' || type === 'user_question') {
			finish('needs-input', 'The agent is asking for a decision. Open the session from the sidebar to answer it.');
		} else if (type === 'done' && state.sessionId) {
			// The feed normally reports completion itself; give it a moment.
			clearTimeout(doneGrace);
			doneGrace = setTimeout(() => finish('done'), 3000);
		}
	}

	try {
		const url = wsUrl('/api/v1/ws/chat', { device_id: `setup-${requestId}` });
		socket = options.openSocket ? options.openSocket(url) : new WebSocket(url);
		socket.onopen = () => {
			try {
				const frame: Record<string, unknown> = { type: 'query', prompt, profile: options.profileId, mode: 'chat', request_id: requestId };
				if (options.model) frame.overrides = { model: options.model };
				socket?.send(JSON.stringify(frame));
			} catch (e) {
				finish('failed', getErrorMessage(e, 'Could not reach the worker.'));
			}
		};
		socket.onmessage = onSocketMessage;
		socket.onerror = () => { if (!state.sessionId) finish('failed', 'Could not reach the worker. Check that it is running, then try again.'); };
		socket.onclose = (event: CloseEvent) => {
			if (settled || state.sessionId) return;
			finish('failed', event.code === 4001 ? 'This browser is not signed in to the worker. Reload the page and sign in.' : 'The connection to the worker closed before the run started.');
		};
	} catch (e) {
		finish('failed', getErrorMessage(e, 'Could not reach the worker.'));
	}

	timeout = setTimeout(() => {
		if (state.text.trim()) finish('done');
		else finish('failed', 'No answer arrived in time. Open the session from the sidebar to see what happened, or try again.');
	}, options.timeoutMs ?? FIRST_RUN_TIMEOUT_MS);
	publish();

	return { done, cancel: () => finish('failed', 'Cancelled.') };
}
