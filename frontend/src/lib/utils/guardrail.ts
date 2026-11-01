/**
 * Provider safety checks, as the chat screen shows them.
 *
 * The backend contract is `docs/internal/guardrail-contract.md`:
 * - a blocked call pauses the turn on an ordinary permission request whose
 *   tool is `ModelGuardrail` (`tool_input.guardrail === true`). Allow means
 *   "Continue this turn on the suggested model", deny means "Stop";
 * - `model_rerouted` (a provider answered with another model) and
 *   `guardrail_blocked` (what happened to a block) are persisted system notes;
 * - `model_info` carries `rerouted` / `reroute_reason` for the model pill.
 *
 * Everything here is pure so the desktop prompt, the phone dock, the queue
 * chips and the transcript notes all word a safety check the same way.
 */
import { getModelLabel } from '$lib/constants/models';

export const GUARDRAIL_TOOL = 'ModelGuardrail';
export const MODEL_REROUTED_SUBTYPE = 'model_rerouted';
export const GUARDRAIL_BLOCKED_SUBTYPE = 'guardrail_blocked';

interface GuardrailLike {
	tool_name?: string | null;
	tool_input?: Record<string, unknown> | null;
}

function text(value: unknown): string {
	return typeof value === 'string' ? value.trim() : '';
}

/** A safety-check prompt, by tool name or by the input's own marker. */
export function isGuardrailRequest(request: GuardrailLike | null | undefined): boolean {
	if (!request) return false;
	return request.tool_name === GUARDRAIL_TOOL || request.tool_input?.guardrail === true;
}

const CATEGORY_WORDS: Readonly<Record<string, string>> = {
	cyber: 'cybersecurity',
	bio: 'biology',
	frontier_llm: 'frontier AI research',
	reasoning_extraction: 'requests for its hidden reasoning',
	content_filter: 'content policy'
};

/** The provider's category in plain words; null when none was named. */
export function guardrailCategoryLabel(category: unknown): string | null {
	const raw = text(category);
	if (!raw) return null;
	return CATEGORY_WORDS[raw] ?? raw.replace(/[_-]+/g, ' ');
}

/** A model id as the pickers name it ("claude-opus-4-8" → its label). */
export function guardrailModelLabel(model: unknown): string {
	const id = text(model);
	return id ? getModelLabel(id) : '';
}

/** "Anthropic's", "OpenAI's", "Fake AI (dev)'s"; "The provider's" when unnamed. */
function possessive(name: string): string {
	if (!name) return "The provider's";
	return /s$/i.test(name) ? `${name}'` : `${name}'s`;
}

export interface GuardrailView {
	providerLabel: string;
	/** "Anthropic's safety check stopped this request" */
	heading: string;
	/** Plain-words category, or null. */
	category: string | null;
	/** The provider's own explanation, or null. */
	explanation: string | null;
	requestedModel: string;
	requestedLabel: string;
	suggestedModel: string;
	suggestedLabel: string;
	/** The app picked the fallback, not the provider. */
	suggestedByApp: boolean;
	/** The provider had streamed part of a reply; it was dropped. */
	partialDiscarded: boolean;
	/** The provider's request id, for support. */
	providerRequestId: string | null;
	/** The primary button: "Continue on Opus 4.8". */
	continueLabel: string;
	/** One line under the buttons on what Continue covers. */
	hint: string;
}

/** Everything the safety-check card shows, or null for any other request. */
export function guardrailView(request: (GuardrailLike & { decision_reason?: string | null }) | null | undefined): GuardrailView | null {
	if (!isGuardrailRequest(request)) return null;
	const input = request?.tool_input ?? {};
	const providerLabel = text(input.provider_label) || text(input.provider);
	const requestedModel = text(input.requested_model);
	const suggestedModel = text(input.suggested_model);
	const requestedLabel = guardrailModelLabel(requestedModel);
	const suggestedLabel = guardrailModelLabel(suggestedModel);
	const explanation = text(input.explanation) || null;
	return {
		providerLabel,
		heading: `${possessive(providerLabel)} safety check stopped this request`,
		category: guardrailCategoryLabel(input.category),
		explanation,
		requestedModel,
		requestedLabel,
		suggestedModel,
		suggestedLabel,
		suggestedByApp: text(input.suggested_by) === 'app',
		partialDiscarded: input.partial_output_discarded === true,
		providerRequestId: text(input.request_id) || null,
		continueLabel: suggestedLabel ? `Continue on ${suggestedLabel}` : 'Continue',
		hint: requestedLabel
			? `Only this turn moves. Your next message goes back to ${requestedLabel}.`
			: 'Only this turn moves. Your next message goes back to the chat’s model.'
	};
}

/** A short label for a queued safety check (the queue chips, a list). */
export function guardrailSummary(request: GuardrailLike): string {
	const view = guardrailView(request);
	if (!view) return '';
	return view.suggestedLabel ? `Continue on ${view.suggestedLabel}?` : 'Continue on another model?';
}

// ---------------------------------------------------------------------------
// Transcript notes
// ---------------------------------------------------------------------------

export type GuardrailNoteKind = 'rerouted' | 'continued' | 'stopped';

export interface GuardrailNoteView {
	kind: GuardrailNoteKind;
	/** Short lead: "Model rerouted", "Safety check". */
	title: string;
	/** The sentence shown after the lead. */
	text: string;
	/** Full sentence from the backend, for the tooltip. */
	detail: string;
}

/** The note for a `model_rerouted` / `guardrail_blocked` system row, else null. */
export function guardrailNoteView(
	subtype: string | null | undefined,
	data: Record<string, unknown> | null | undefined,
	fallbackContent?: string | null
): GuardrailNoteView | null {
	const d = data ?? {};
	const content = text(d.content) || text(fallbackContent);
	if (subtype === MODEL_REROUTED_SUBTYPE) {
		const served = guardrailModelLabel(d.served_model);
		const requested = guardrailModelLabel(d.requested_model);
		const provider = text(d.provider_label);
		const category = guardrailCategoryLabel(d.category);
		// Friendly labels when both models are known; the backend's own
		// sentence (raw ids) otherwise, and always in the tooltip.
		const sentence =
			served && requested
				? `${provider || 'The provider'} answered with ${served} instead of ${requested}${category ? ` (safety check: ${category})` : ''}.`
				: content || 'The provider answered with a different model.';
		return {
			kind: 'rerouted',
			title: text(d.reason) === 'refusal_fallback' || category ? 'Safety check' : 'Model rerouted',
			text: sentence,
			detail: content || sentence
		};
	}
	if (subtype === GUARDRAIL_BLOCKED_SUBTYPE) {
		const outcome = text(d.outcome);
		const provider = text(d.provider_label);
		const category = guardrailCategoryLabel(d.category);
		const what = `${possessive(provider)} safety check blocked the request${category ? ` (${category})` : ''}`;
		if (outcome === 'continued') {
			const retry = guardrailModelLabel(d.retry_model || d.suggested_model);
			return {
				kind: 'continued',
				title: 'Safety check',
				text: retry ? `Continued on ${retry} — ${lowerFirst(what)}.` : `Continued — ${lowerFirst(what)}.`,
				detail: content || `${what}.`
			};
		}
		const reason = stopReason(d.reason);
		return {
			kind: 'stopped',
			title: 'Safety check',
			text: `Stopped — ${lowerFirst(what)}${reason ? `; ${reason}` : ''}.`,
			detail: content || `${what}. The turn stopped.`
		};
	}
	return null;
}

function lowerFirst(value: string): string {
	return value.startsWith('The provider') ? value.charAt(0).toLowerCase() + value.slice(1) : value;
}

/** The backend's stop reason, said plainly (null when it is the user's own Stop). */
function stopReason(reason: unknown): string | null {
	const raw = text(reason).replace(/\.$/, '');
	if (!raw) return null;
	if (/user denied/i.test(raw)) return 'you chose Stop';
	if (/timed out/i.test(raw)) return 'nobody answered in time';
	return raw.charAt(0).toLowerCase() + raw.slice(1);
}

// ---------------------------------------------------------------------------
// The model pill
// ---------------------------------------------------------------------------

/** Tooltip wording for a reroute, given the backend's reason when known. */
export function rerouteTooltip(actualLabel: string, requestedLabel: string, reason: string | null | undefined): string {
	const running = `running ${actualLabel} instead of ${requestedLabel}`;
	if (reason === 'refusal_fallback') return `Safety check — ${running}`;
	if (reason === 'availability') return `Model unavailable — ${running}`;
	return `Model rerouted: ${running}`;
}
