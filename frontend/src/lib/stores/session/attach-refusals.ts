import { SvelteMap } from 'svelte/reactivity';

/** Late attach failures belong to the chat, not the sheet that has unmounted. */
export const attachRefusals = new SvelteMap<string, string>();
const attempts = new Map<string, symbol>();

/** A remounted sheet can retry before the first request returns. */
export function beginAttach(chat: string) {
	const token = Symbol();
	attempts.set(chat, token);
	attachRefusals.delete(chat);
	const current = () => attempts.get(chat) === token;
	return { current, finish: () => { if (current()) attempts.delete(chat); } };
}
