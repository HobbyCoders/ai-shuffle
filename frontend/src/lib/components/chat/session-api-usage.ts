/**
 * The paid API account a chat's media tools last billed, read from its
 * transcript, so the usage popover's collapsed "API keys" group can show that
 * account's balance (fal after generating images, for example).
 *
 * The generation tools (generateImage, generateVideo, generateAudio, textTo3D,
 * runFalModel) return the resolver's result, whose `choice` names the source
 * and the account it ran on: `fal` / `fal-ai` (the fal usage row's id), or an
 * OpenAI / xAI API-key provider row. Plan sources (ChatGPT, SuperGrok) are
 * subscriptions, not paid APIs, so they are skipped. The older fal-only tools
 * (editImage, imageToVideo, ...) carry no `choice`: a successful one ran on fal
 * unless it names another provider (a ChatGPT plan edit says so).
 */
import type { ChatMessage } from '$lib/stores/types';

/** The fal usage row's id (app.core.provider_usage.FAL_USAGE_PROVIDER_ID). */
export const FAL_USAGE_ACCOUNT = 'fal-ai';

const PAID_API_SOURCES = new Set(['fal', 'openai', 'xai']);
const FAL_ONLY_TOOLS = new Set([
	'editImage',
	'imageToVideo',
	'extendVideo',
	'generateWithReference',
	'imageTo3D',
	'audioToAudio',
]);
const AI_TOOL_NAME = /(?:^|__)ai-tools__([A-Za-z0-9]+)$/;

function parseResult(text: string | undefined): Record<string, unknown> | null {
	if (!text) return null;
	try {
		const value: unknown = JSON.parse(text.trim());
		return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
	} catch {
		return null;
	}
}

/**
 * One paid charge: the account billed and the worker that ran the call — the
 * resolver's `choice.worker` (a device id, or "local" on an install with no
 * network identity). Null when the result does not say (the fal-only tools,
 * which run on the chat's own worker).
 */
export interface PaidApiCharge {
	account: string;
	worker: string | null;
}

/** What one finished tool call billed, or null when it billed no paid API. */
export function paidApiChargeOf(message: ChatMessage): PaidApiCharge | null {
	if (message.type !== 'tool_use' || message.toolStatus !== 'complete') return null;
	const tool = AI_TOOL_NAME.exec(message.toolName ?? '')?.[1];
	if (!tool) return null;
	const result = parseResult(message.toolResult);
	if (!result) return null;
	const choice = result.choice;
	if (choice && typeof choice === 'object') {
		const { source, account, worker } = choice as Record<string, unknown>;
		if (result.status !== 'completed' || typeof source !== 'string' || !PAID_API_SOURCES.has(source)) return null;
		const billed = typeof account === 'string' && account ? account : source === 'fal' ? FAL_USAGE_ACCOUNT : null;
		return billed ? { account: billed, worker: typeof worker === 'string' && worker ? worker : null } : null;
	}
	if (!FAL_ONLY_TOOLS.has(tool) || result.success !== true) return null;
	const used = result.provider_used;
	return typeof used !== 'string' || /fal/i.test(used) ? { account: FAL_USAGE_ACCOUNT, worker: null } : null;
}

/** The account one finished tool call billed, or null when it billed no paid API. */
export function paidApiAccountOf(message: ChatMessage): string | null {
	return paidApiChargeOf(message)?.account ?? null;
}

/** The chat's most recent paid charge, or null. */
export function lastPaidApiCharge(messages: readonly ChatMessage[]): PaidApiCharge | null {
	for (let i = messages.length - 1; i >= 0; i--) {
		const charge = paidApiChargeOf(messages[i]);
		if (charge) return charge;
	}
	return null;
}

/** The account the chat's most recent paid tool call billed, or null. */
export function lastPaidApiAccount(messages: readonly ChatMessage[]): string | null {
	return lastPaidApiCharge(messages)?.account ?? null;
}
