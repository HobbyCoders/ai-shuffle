import { describe, it, expect } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import { galleryMediaForTurn, rewriteWithGallery } from './media-gallery';
import { buildDisplayItems } from './work-log';

const image = '/api/generated-images/by-path?path=/images/a.png';
const image2 = '/api/generated-images/by-path?path=/images/b.png';
const message = (id: string, props: Partial<ChatMessage>): ChatMessage => ({ id, role: 'assistant', content: '', ...props });
const user = message('u', { role: 'user', content: 'Make images' });
const tool = message('t', { type: 'tool_use', toolName: 'Read', toolStatus: 'complete', toolScreenshot: image });
const answer = message('a', { type: 'text', content: `My choices.

![First](${image})

![Second](${image2})` });
const options = { isStreaming: false, groupingEnabled: true };

describe('settled turn media gallery', () => {
	it('keeps gallery, inline suppression and downloads on the owning session', () => {
		const items = buildDisplayItems([user, tool, answer], { ...options, sessionId: 'remote-chat' });
		const gallery = items.find(item => item.kind === 'work_log')?.gallery ?? [];
		expect(gallery).toHaveLength(2);
		for (const item of gallery) {
			expect(new URL(item.src, 'https://viewer').searchParams.get('session_id')).toBe('remote-chat');
		}
		const html = rewriteWithGallery(answer.content, new Set(gallery.map(item => item.src)), 'remote-chat');
		expect(html).toContain('My choices.');
		expect(html).not.toContain('<img');
	});
	it('ignores markdown examples in fenced and inline code', () => {
		expect(galleryMediaForTurn([message('code', { content: '`![sample](/a.png)`\n\n```md\n![sample](/b.png)\n```' })])).toEqual([]);
	});
	it('deduplicates screenshots and markdown in original order', () => {
		const gallery = galleryMediaForTurn([tool, answer]);
		expect(gallery.map(item => item.src)).toEqual([image, image2]);
	});
	it('keeps one image inline and excludes user attachments and raw tool results', () => {
		expect(galleryMediaForTurn([tool])).toEqual([]);
		expect(galleryMediaForTurn([message('u', { role: 'user', content: answer.content }), message('t', { type: 'tool_use', toolResult: JSON.stringify({ success: true, image_url: image }) })])).toEqual([]);
	});
	it('collects video links but leaves audio, ordinary links and prose in chat', () => {
		const text = `Choose this. ![Photo](${image}) [Clip](/demo.mp4) [Audio](/demo.mp3) [File](/notes.txt)`;
		const gallery = galleryMediaForTurn([message('a', { content: text })]);
		expect(gallery.map(item => item.kind)).toEqual(['image', 'video']);
		const rendered = rewriteWithGallery(text, new Set(gallery.map(item => item.src)));
		expect(rendered).toContain('Choose this.');
		expect(rendered).toContain('<audio');
		expect(rendered).toContain('[File](/notes.txt)');
		expect(rendered).not.toContain('<img');
		expect(rendered).not.toContain('<video');
	});
	it('rejects active URL schemes', () => {
		expect(galleryMediaForTurn([message('bad', { content: '![Bad](javascript:alert.png) ![Bad](file:///tmp/b.png)' })])).toEqual([]);
	});
	it('keeps live turns untouched, then groups screenshots on completion', () => {
		const messages = [user, tool, answer];
		expect(buildDisplayItems(messages, { ...options, isStreaming: true }).some(item => item.kind === 'work_log' && item.gallery?.length)).toBe(false);
		const settled = buildDisplayItems(messages, options).find(item => item.kind === 'work_log');
		expect(settled?.kind).toBe('work_log');
		if (settled?.kind !== 'work_log') throw new Error('Missing group');
		expect(settled.gallery).toHaveLength(2);
		expect(settled.sequence.find(entry => entry.message.id === 't')?.hiddenWhenCollapsed).toBe(true);
		expect(settled.sequence.find(entry => entry.message.id === 'a')?.hiddenWhenCollapsed).toBe(false);
		expect(answer.content).toContain(image);
	});
	it('settles an earlier turn when the next user message arrives, without mixing turns', () => {
		const items = buildDisplayItems([user, tool, answer, message('u2', { role: 'user', content: 'Next' }), message('a2', { content: `![single](${image2})` })], { ...options, isStreaming: true });
		// The live turn is one live work log; only the earlier turn is settled with a gallery.
		const logs = items.filter(item => item.kind === 'work_log');
		const groups = logs.filter(item => item.mode === 'settled');
		expect(groups).toHaveLength(1);
		expect(groups[0].gallery).toHaveLength(2);
		expect(logs.filter(item => item.mode === 'live').every(item => !item.gallery?.length)).toBe(true);
	});
	it('splits galleries at same-turn steers only after the entire turn settles', () => {
		const steer = message('steer', { role: 'user', turnId: 'turn-1', content: 'Try a different style' });
		const later = message('later', { type: 'text', content: '![Third](/c.png) [Clip](/d.mp4)' });
		const messages = [{ ...user, turnId: 'turn-1' }, tool, answer, steer, later];
		for (let length = 1; length <= messages.length; length++) {
			for (const liveOptions of [{ ...options, isStreaming: true }, { ...options, latestTurnUnsettled: true }]) {
				const items = buildDisplayItems(messages.slice(0, length), liveOptions);
				// Live: at most the turn's one live work log, never a settled fold or a gallery.
				expect(items.some(item => item.kind === 'work_log' && (item.mode === 'settled' || item.gallery?.length))).toBe(false);
			}
		}
		const items = buildDisplayItems(messages, options);
		// One fold for the turn. Its gallery sits above the kept rows, so only
		// media from before the steer joins it; later media stays in its row.
		expect(items.map(item => item.kind)).toEqual(['message', 'work_log']);
		const groups = items.filter(item => item.kind === 'work_log');
		expect(groups.map(group => group.gallery?.map(media => media.src)))
			.toEqual([[image, image2]]);
		expect(groups.map(group => group.sequence.map(entry => entry.message.id)))
			.toEqual([['t', 'a', 'steer', 'later']]);
		expect(groups[0].sequence.filter(entry => !entry.hiddenWhenCollapsed).map(entry => entry.message.id))
			.toEqual(['a', 'steer', 'later']);
		expect(buildDisplayItems(structuredClone(messages), options)).toEqual(items);
	});
	it('does not combine single images across a steer into a gallery', () => {
		const messages = [
			{ ...user, turnId: 'turn-1' },
			message('before', { type: 'text', content: '![Before](/before.png)' }),
			message('steer', { role: 'user', turnId: 'turn-1', content: 'Change it' }),
			message('after', { type: 'text', content: '![After](/after.png)' })
		];
		expect(buildDisplayItems(messages, options).map(item => item.kind))
			.toEqual(['message', 'message', 'message', 'message']);
	});
	it('does not override disabled work grouping', () => {
		expect(buildDisplayItems([user, tool, answer], { ...options, groupingEnabled: false }).every(item => item.kind !== 'work_log')).toBe(true);
	});
	it('groups a media-only answer even without routine tools', () => {
		expect(buildDisplayItems([user, answer], options).find(item => item.kind === 'work_log')?.kind).toBe('work_log');
	});
});
