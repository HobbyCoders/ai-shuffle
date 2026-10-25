import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { createPromptRecall, promptHistory } from './prompt-recall';
import { createPromptStash, draftPreview, stashAge, stashStorageKey, MAX_STASHED_PROMPTS } from './prompt-stash';
import { createPlainPasteIntent, isLargePaste, LARGE_PASTE_BYTES, pastedTextFile } from '$lib/components/chat/tiptap/large-paste';

describe('promptHistory', () => {
	it('lists user text prompts newest first and folds repeats', () => {
		expect(promptHistory([
			{ role: 'user', content: 'a' },
			{ role: 'assistant', content: 'reply' },
			{ role: 'user', content: 'b' },
			{ role: 'user', content: 'b' },
			{ role: 'user', content: '/compact', type: 'command' },
			{ role: 'user', content: '   ' },
			{ role: 'user', content: 'c' }
		])).toEqual(['c', 'b', 'a']);
	});
});

describe('createPromptRecall', () => {
	const history = ['newest', 'older', 'oldest'];

	it('starts only from an empty composer with ArrowUp', () => {
		const recall = createPromptRecall();
		expect(recall.step('down', history, true)).toBeNull();
		expect(recall.step('up', history, false)).toBeNull();
		expect(recall.step('up', [], true)).toBeNull();
		expect(recall.step('up', history, true)).toEqual({ text: 'newest' });
		expect(recall.active).toBe(true);
	});

	it('stops at the oldest prompt and walks back to empty', () => {
		const recall = createPromptRecall();
		recall.step('up', history, true);
		recall.step('up', history, false);
		expect(recall.step('up', history, false)).toEqual({ text: 'oldest' });
		expect(recall.step('up', history, false)).toEqual({ text: 'oldest' });
		expect(recall.step('down', history, false)).toEqual({ text: 'older' });
		expect(recall.step('down', history, false)).toEqual({ text: 'newest' });
		expect(recall.step('down', history, false)).toEqual({ text: '' });
		expect(recall.active).toBe(false);
	});

	it('lets go on reset (the user edited)', () => {
		const recall = createPromptRecall();
		recall.step('up', history, true);
		recall.reset();
		expect(recall.step('up', history, false)).toBeNull();
	});
});

describe('prompt stash', () => {
	beforeEach(() => localStorage.clear());

	const doc = (text: string) => JSON.stringify({ type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text }] }] });

	it('keeps each account’s list separate and persisted', () => {
		const stash = createPromptStash(() => 1000);
		stash.setOwner('user:a');
		stash.push(doc('for a'));
		stash.setOwner('user:b');
		expect(get(stash)).toEqual([]);
		stash.push('for b');
		stash.setOwner('user:a');
		expect(get(stash).map(e => e.preview)).toEqual(['for a']);
		expect(JSON.parse(localStorage.getItem(stashStorageKey('user:b'))!)[0].draft).toBe('for b');
	});

	it('refuses empty drafts and keeps nothing while signed out', () => {
		const stash = createPromptStash();
		stash.setOwner('user:a');
		expect(stash.push('')).toBeNull();
		expect(stash.push(JSON.stringify({ type: 'doc', content: [{ type: 'paragraph' }] }))).toBeNull();
		stash.setOwner(null);
		// Signed out nothing can be kept, so the push says so and lists nothing.
		expect(stash.push('signed out')?.durable).toBe(false);
		expect(get(stash)).toEqual([]);
		expect(localStorage.length).toBe(0);
	});

	it('takes, puts back in time order, and caps the list', () => {
		let t = 0;
		const stash = createPromptStash(() => ++t);
		stash.setOwner('user:a');
		const first = stash.push('one')!;
		expect(first.durable).toBe(true);
		stash.push('two');
		const taken = stash.find(first.entry.id)!;
		expect(stash.take(first.entry.id)).toBe(true);
		expect(stash.take(first.entry.id)).toBe(false);
		expect(get(stash).map(e => e.preview)).toEqual(['two']);
		expect(stash.putBack(taken)).toBe(true);
		expect(get(stash).map(e => e.preview)).toEqual(['two', 'one']);
		for (let i = 0; i < MAX_STASHED_PROMPTS + 5; i++) stash.push(`p${i}`);
		expect(get(stash)).toHaveLength(MAX_STASHED_PROMPTS);
	});

	it('swaps an entry for a draft without evicting another from a full stash', () => {
		let t = 0;
		const stash = createPromptStash(() => ++t);
		stash.setOwner('user:a');
		for (let i = 0; i < MAX_STASHED_PROMPTS; i++) stash.push(`p${i}`);
		const restored = get(stash).find(e => e.preview === 'p10')!;
		const swapped = stash.swap(restored.id, 'current draft');
		expect(swapped?.durable).toBe(true);
		const previews = get(stash).map(e => e.preview);
		expect(previews).toHaveLength(MAX_STASHED_PROMPTS);
		expect(previews[0]).toBe('current draft');
		expect(previews).not.toContain('p10');
		// The oldest entry survives: nothing was evicted to make room.
		expect(previews).toContain('p0');
		expect(JSON.parse(localStorage.getItem(stashStorageKey('user:a'))!)).toHaveLength(MAX_STASHED_PROMPTS);
		expect(stash.swap('missing', 'x')).toBeNull();
		expect(stash.swap(swapped!.entry.id, '')).toBeNull();
	});

	describe('when the browser refuses to save', () => {
		afterEach(() => vi.restoreAllMocks());

		it('reports the push as not durable and leaves the list as it was', () => {
			const stash = createPromptStash();
			stash.setOwner('user:a');
			stash.push('kept');
			vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError'); });
			const pushed = stash.push('lost?');
			expect(pushed?.durable).toBe(false);
			expect(get(stash).map(e => e.preview)).toEqual(['kept']);
			expect(JSON.parse(localStorage.getItem(stashStorageKey('user:a'))!).map((e: { draft: string }) => e.draft)).toEqual(['kept']);
		});

		it('keeps a taken entry listed when its removal is not saved, and reports putBack', () => {
			const stash = createPromptStash();
			stash.setOwner('user:a');
			const { entry } = stash.push('two')!;
			stash.push('one');
			vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('disabled'); });
			expect(stash.take(entry.id)).toBe(false);
			expect(get(stash).map(e => e.preview)).toEqual(['one', 'two']);
			vi.restoreAllMocks();
			expect(stash.take(entry.id)).toBe(true);
			vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('disabled'); });
			expect(stash.putBack(entry)).toBe(false);
			expect(get(stash).map(e => e.preview)).toEqual(['one']);
		});

		it('leaves the list untouched when a swap is not saved', () => {
			const stash = createPromptStash();
			stash.setOwner('user:a');
			const { entry } = stash.push('stashed')!;
			vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('disabled'); });
			expect(stash.swap(entry.id, 'draft')?.durable).toBe(false);
			expect(get(stash).map(e => e.preview)).toEqual(['stashed']);
		});
	});

	it('ignores a corrupt stored list', () => {
		localStorage.setItem(stashStorageKey('user:a'), '{not json');
		const stash = createPromptStash();
		stash.setOwner('user:a');
		expect(get(stash)).toEqual([]);
	});

	it('previews chips by name on one line', () => {
		const draft = JSON.stringify({
			type: 'doc',
			content: [
				{ type: 'paragraph', content: [{ type: 'text', text: 'look at' }, { type: 'fileChip', attrs: { filename: 'a.ts' } }] },
				{ type: 'paragraph', content: [{ type: 'text', text: 'please' }] }
			]
		});
		expect(draftPreview(draft)).toBe('look at@a.ts please');
		expect(draftPreview('x'.repeat(200), 10)).toBe(`${'x'.repeat(9)}…`);
	});

	it('describes the stash time', () => {
		expect(stashAge(0, 30_000)).toBe('just now');
		expect(stashAge(0, 5 * 60_000)).toBe('5 min ago');
		expect(stashAge(0, 3 * 3_600_000)).toBe('3 h ago');
	});
});

describe('large paste', () => {
	it('measures the threshold in UTF-8 bytes', () => {
		expect(isLargePaste('a'.repeat(LARGE_PASTE_BYTES - 1))).toBe(false);
		expect(isLargePaste('a'.repeat(LARGE_PASTE_BYTES))).toBe(true);
		// 3 bytes per character: well under the length, over the byte count.
		expect(isLargePaste('€'.repeat(Math.ceil(LARGE_PASTE_BYTES / 3)))).toBe(true);
	});

	it('names the attachment from the paste time', () => {
		const file = pastedTextFile('hello', new Date(2026, 9, 2, 10, 15, 30));
		expect(file.name).toBe('pasted-text-20261002-101530.txt');
		expect(file.type).toBe('text/plain');
	});

	it('arms plain paste only for Ctrl/Cmd+Shift+V, once', () => {
		let now = 0;
		const intent = createPlainPasteIntent(() => now);
		intent.keydown({ key: 'v', ctrlKey: true, shiftKey: false, metaKey: false, altKey: false });
		expect(intent.consume()).toBe(false);
		intent.keydown({ key: 'V', ctrlKey: false, shiftKey: true, metaKey: true, altKey: false });
		expect(intent.consume()).toBe(true);
		expect(intent.consume()).toBe(false);
		intent.keydown({ key: 'V', ctrlKey: true, shiftKey: true, metaKey: false, altKey: false });
		now = 5000;
		expect(intent.consume()).toBe(false);
	});
});
