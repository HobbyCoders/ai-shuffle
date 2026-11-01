/**
 * Approvals an owner chat asks in words, not with a dialog.
 *
 * A permission request marked `assistant_confirm` is projected as one card in
 * the transcript, keyed by its request id, at the point it was raised, and
 * settled in place (authorized, declined, expired, cancelled). The owner
 * answers in the message box ("yes" / "no"); deciding what the reply means is
 * the backend's job. The card projection here must match the backend's REST
 * fold (`_approval_card` in app/api/sessions_helpers.py) so live, replay and
 * reload read the same transcript.
 */
import type { PermissionRequest } from '$lib/api/client';
import type { ChatMessage } from '$lib/stores/types';
import { displayToolName } from './tool-icons';

function text(value: unknown): string {
	return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
}

function clip(value: string, max = 120): string {
	return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function code(value: string): string {
	// A backtick inside would end the inline code early.
	return `\`${clip(value).replace(/`/g, "'")}\``;
}

/** What the request would do, as the object of "May I …". */
function action(request: Pick<PermissionRequest, 'tool_name' | 'tool_input'>): string {
	const name = displayToolName(request.tool_name ?? '');
	const input = request.tool_input ?? {};
	const command = text(input.command);
	const path = text(input.file_path) || text(input.path) || text(input.notebook_path);
	const url = text(input.url);
	switch (name) {
		case 'Bash':
		case 'BashOutput':
		case 'Monitor':
			return command ? `run ${code(command)}` : 'run a command';
		case 'Write':
			return path ? `write ${code(path)}` : 'write a file';
		case 'Edit':
		case 'MultiEdit':
		case 'NotebookEdit':
			return path ? `change ${code(path)}` : 'change a file';
		case 'WebFetch':
			return url ? `open ${url}` : 'open a web page';
		default: {
			const description = text(input.description);
			if (description) return clip(description.charAt(0).toLowerCase() + description.slice(1));
			return name ? `use ${name}` : 'go ahead';
		}
	}
}

/** "May I run `npm test`? Yes or no." — used when the server sent no wording. */
export function assistantConfirmationText(request: Pick<PermissionRequest, 'tool_name' | 'tool_input'>): string {
	return `May I ${action(request)}? Yes or no.`;
}

/** A request the assistant asks in words; it never opens the approval dialog. */
export function isAssistantConfirm(request: Pick<PermissionRequest, 'assistant_confirm'>): boolean {
	return !!request.assistant_confirm && typeof request.assistant_confirm === 'object';
}

/**
 * The one question on screen: the oldest in-conversation request (the server
 * settles them one at a time, oldest first), in the server's own words.
 */
export function currentAssistantQuestion(requests: readonly PermissionRequest[]): { request: PermissionRequest; text: string } | null {
	const request = requests.find(isAssistantConfirm);
	if (!request) return null;
	const question = request.assistant_confirm?.question?.trim();
	return { request, text: question || assistantConfirmationText(request) };
}

// ---------------------------------------------------------------------------
// The request as one card in the transcript (#483)
// ---------------------------------------------------------------------------
//
// A request asked in words is shown once, by the app, at the point it was
// raised: the durable `interaction.requested` event makes a card keyed by its
// request id, and `interaction.answered` / `interaction.expired` settle that
// same card in place. Nothing floats after the transcript and the model does
// not repeat it, so two jobs asking at once read as two cards in order, each
// keeping its own outcome. The backend's REST fold (`load_event_messages`)
// projects the same card, so a reload and every client agree.

/** The transcript row for a request asked in words. */
export const APPROVAL_CARD_SUBTYPE = 'assistant_confirm_request';
/** The outcome row the backend writes after the owner's answer. */
export const APPROVAL_OUTCOME_SUBTYPE = 'assistant_confirm';

export type ApprovalStatus = 'pending' | 'approved' | 'declined' | 'expired' | 'cancelled';

export interface ApprovalCardData {
	request_id: string;
	question: string;
	action?: string;
	asker?: string;
	chat_session_id?: string;
	status: ApprovalStatus;
	/** 1 for a first ask; 2+ when earlier cards asking the same thing were declined or expired. */
	attempt: number;
	/** A provider safety check (`ModelGuardrail`): what the card shows instead of the raw call. */
	guardrail?: ApprovalGuardrail;
}

/** The safety-check fields an approval card keeps (docs/internal/guardrail-contract.md). */
export interface ApprovalGuardrail {
	provider?: string;
	provider_label?: string;
	category?: string;
	explanation?: string;
	requested_model?: string;
	suggested_model?: string;
	suggested_by?: string;
	partial_output_discarded?: boolean;
}

const GUARDRAIL_TEXT_FIELDS = [
	'provider',
	'provider_label',
	'category',
	'explanation',
	'requested_model',
	'suggested_model',
	'suggested_by'
] as const;

/**
 * The safety-check part of a request, or null for any other request. Kept
 * here (not in utils/guardrail.ts) because this module is bundled into the
 * server's transcript reducer, which cannot load the model registry. Must
 * match `_approval_guardrail` in app/api/sessions_helpers.py.
 */
export function approvalGuardrail(request: Record<string, unknown> | undefined): ApprovalGuardrail | null {
	const input = record(request?.tool_input);
	if (!input || (text(request?.tool_name) !== 'ModelGuardrail' && input.guardrail !== true)) return null;
	const out: Record<string, unknown> = {};
	for (const key of GUARDRAIL_TEXT_FIELDS) {
		const value = text(input[key]);
		if (value) out[key] = value;
	}
	if (input.partial_output_discarded === true) out.partial_output_discarded = true;
	return out as ApprovalGuardrail;
}

/** One card per request id: the id is stable across live, replay and reload. */
export function approvalCardId(requestId: string): string {
	return `assistant-confirm-${requestId}`;
}

function record(value: unknown): Record<string, unknown> | undefined {
	return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : undefined;
}

export function isApprovalCard(message: Pick<ChatMessage, 'type' | 'systemSubtype'>): boolean {
	return message.type === 'system' && message.systemSubtype === APPROVAL_CARD_SUBTYPE;
}

export function isApprovalOutcome(message: Pick<ChatMessage, 'type' | 'systemSubtype'>): boolean {
	return message.type === 'system' && message.systemSubtype === APPROVAL_OUTCOME_SUBTYPE;
}

export function approvalCardData(message: Pick<ChatMessage, 'systemData'>): ApprovalCardData | null {
	const data = record(message.systemData);
	const requestId = text(data?.request_id);
	if (!data || !requestId) return null;
	return data as unknown as ApprovalCardData;
}

/** Earlier outcomes that make a same-question card a retry ("Asked again"). */
const RETRIED_STATUSES: ReadonlySet<ApprovalStatus> = new Set(['declined', 'expired']);

/**
 * The card for a durable `interaction.requested` event, or null when the
 * request is not asked in words. `earlier` is the transcript so far: a card
 * that repeats an earlier request which was declined or expired is numbered,
 * so a retry never looks like the request it replaces. Running an approved
 * action again is a new first ask.
 */
export function approvalCardFromRequest(
	requestId: string,
	request: Record<string, unknown> | undefined,
	earlier: readonly ChatMessage[]
): ApprovalCardData | null {
	const confirm = record(request?.assistant_confirm);
	if (!requestId || !confirm) return null;
	const question =
		text(confirm.question) ||
		assistantConfirmationText({
			tool_name: text(request?.tool_name),
			tool_input: (record(request?.tool_input) ?? {}) as PermissionRequest['tool_input']
		});
	let attempt = 1;
	for (const message of earlier) {
		if (!isApprovalCard(message)) continue;
		const card = approvalCardData(message);
		if (card?.question === question && RETRIED_STATUSES.has(card.status)) attempt++;
	}
	const guardrail = approvalGuardrail(request);
	return {
		request_id: requestId,
		question,
		...(text(confirm.action) ? { action: text(confirm.action) } : {}),
		...(text(confirm.asker) ? { asker: text(confirm.asker) } : {}),
		...(text(confirm.chat_session_id) ? { chat_session_id: text(confirm.chat_session_id) } : {}),
		status: 'pending',
		attempt,
		...(guardrail ? { guardrail } : {})
	};
}

/**
 * Where a settled request ends up, from `interaction.answered` (its `answer`)
 * or `interaction.expired` (its `reason`). Null leaves the card as it is: a
 * deferred answer is re-asked under the same request id.
 */
export function approvalStatusFromSettle(
	type: 'interaction.answered' | 'interaction.expired',
	detail: Record<string, unknown> | undefined
): ApprovalStatus | null {
	if (type === 'interaction.expired') return text(detail?.reason) === 'ttl' ? 'expired' : 'cancelled';
	const decision = text(detail?.decision) || text(detail?.behavior);
	if (decision === 'allow') return 'approved';
	if (decision === 'deny') return 'declined';
	return null;
}
