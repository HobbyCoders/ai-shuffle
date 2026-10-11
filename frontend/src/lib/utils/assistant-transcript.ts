/**
 * Assistant-mode transcript: the assistant's own words first.
 *
 * In the assistant's home conversation the reader sees their prompts, the
 * assistant's prose and its worker cards. Everything else a turn produced (tool
 * rows, thinking, work-log groups, Task and swarm tool rows, system and
 * permission notices, agent deliveries) moves into one collapsed "Details" item
 * at the end of that turn, in its original order. A settled turn applies the
 * same protected set as the ordinary fold (transcript-organization spec §3):
 * narration the fold hides goes to Details, findings and questions stay.
 * Ordinary chats never pass through here.
 */
import type { ChatMessage } from '$lib/stores/types';
import type { DisplayItem, WorkLogEntry } from './work-log';
import { agentDelivery } from './agent-delivery';
import { isNavigableUserTurn } from './transcript-navigation';

export interface AssistantDetailsItem {
	kind: 'details';
	key: string;
	/** Hidden rows, in transcript order, rendered by the shared row snippet. */
	entries: WorkLogEntry[];
	/** True for the latest turn while it is still running. */
	live: boolean;
}

export type AssistantViewItem = DisplayItem | AssistantDetailsItem;

export interface AssistantTranscript {
	items: AssistantViewItem[];
	/** Same contract as TranscriptModel.itemIndexByTurnId, for the folded list. */
	itemIndexByTurnId: Map<string, number>;
}

/** `card` is a worker card: it stays in line like prose. */
type Role = 'prompt' | 'spoken' | 'card' | 'detail';

/** Where a message goes in assistant mode. */
export function assistantRowRole(message: ChatMessage): Role {
	if (agentDelivery(message)) return 'detail';
	if (message.type === 'command') return 'prompt';
	if (message.role === 'user') return 'prompt';
	if (message.type === 'text' || !message.type) return 'spoken';
	if (message.type === 'subagent') return 'card';
	return 'detail';
}

interface Entry {
	entry: WorkLogEntry;
	original?: DisplayItem;
	/** Set for a settled turn's rows: the ordinary fold hides this one. */
	hidden?: boolean;
}

function entriesOf(item: DisplayItem): Entry[] {
	switch (item.kind) {
		case 'message':
			return [{ entry: { message: item.message, index: item.index }, original: item }];
		case 'system_group':
			return item.messages.map((message, i) => ({ entry: { message, index: item.indices[i] ?? -1 } }));
		case 'work_log':
			// A settled turn carries its full order in `sequence`; a live one splits
			// it into the folded rows and the visible tail.
			if (item.sequence.length) {
				return item.sequence.map(({ message, index, hiddenWhenCollapsed }) => ({
					entry: { message, index },
					hidden: !!hiddenWhenCollapsed
				}));
			}
			return [...item.folded, ...item.tail].map(({ message, index }) => ({ entry: { message, index } }));
	}
}

/** A row the settled fold hides goes to Details, whatever its role. */
function roleOf({ entry, hidden }: Entry): Role {
	const role = assistantRowRole(entry.message);
	return hidden && role !== 'prompt' ? 'detail' : role;
}

/**
 * Fold an ordinary display list into the assistant-mode list.
 * `live` marks the last turn as still running.
 */
export function foldAssistantTranscript(items: readonly DisplayItem[], live = false): AssistantTranscript {
	const out: AssistantViewItem[] = [];
	let details: WorkLogEntry[] = [];

	const flush = (isLast: boolean) => {
		if (!details.length) return;
		const first = details[0];
		out.push({
			kind: 'details',
			key: `details-${first.message.id || first.index}`,
			entries: details,
			live: isLast && live
		});
		details = [];
	};

	for (const item of items) {
		for (const row of entriesOf(item)) {
			const { entry, original } = row;
			const role = roleOf(row);
			if (role === 'detail') {
				details.push(entry);
				continue;
			}
			if (role === 'prompt') flush(false);
			out.push(original ?? { kind: 'message', message: entry.message, index: entry.index });
		}
	}
	flush(true);

	const itemIndexByTurnId = new Map<string, number>();
	out.forEach((item, index) => {
		if (item.kind !== 'message') return;
		const { message } = item;
		if (message.id && isNavigableUserTurn(message) && !itemIndexByTurnId.has(message.id)) {
			itemIndexByTurnId.set(message.id, index);
		}
	});
	return { items: out, itemIndexByTurnId };
}

/** True while the assistant's own words are streaming (they carry their own cursor). */
export function isStreamingProse(message: ChatMessage | undefined): boolean {
	return !!message?.streaming && !!message.content && assistantRowRole(message) === 'spoken';
}
