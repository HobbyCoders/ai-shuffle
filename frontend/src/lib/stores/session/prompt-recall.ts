/**
 * ArrowUp in an empty composer recalls this session's earlier prompts, like a
 * shell history. Text only: attachments are not re-attached. Once the user
 * edits a recalled prompt, recall lets go and the arrows move the cursor again.
 */

interface PromptLike {
	role: string;
	content: string;
	type?: string;
}

/** The session's user prompts, newest first, with consecutive repeats folded. */
export function promptHistory(messages: readonly PromptLike[]): string[] {
	const out: string[] = [];
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i];
		if (m.role !== 'user' || (m.type && m.type !== 'text')) continue;
		const text = m.content?.trim();
		if (!text || out[out.length - 1] === text) continue;
		out.push(text);
	}
	return out;
}

export type RecallStep = { text: string } | null;

/**
 * The recall cursor. `step` returns the text to show, or null when the key
 * should do its normal job. Leaving the newest entry with ArrowDown returns ''
 * (the empty composer recall started from).
 */
export function createPromptRecall() {
	let index = -1;
	return {
		get active() {
			return index >= 0;
		},
		step(direction: 'up' | 'down', history: readonly string[], composerEmpty: boolean): RecallStep {
			if (index < 0) {
				if (direction !== 'up' || !composerEmpty || history.length === 0) return null;
				index = 0;
				return { text: history[0] };
			}
			if (direction === 'up') {
				if (index >= history.length - 1) return { text: history[index] };
				index++;
				return { text: history[index] };
			}
			index--;
			return { text: index < 0 ? '' : history[index] };
		},
		/** The user typed, pasted, sent or switched sessions. */
		reset() {
			index = -1;
		}
	};
}
