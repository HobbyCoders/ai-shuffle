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
		const groups = items.filter(item => item.kind === 'work_log');
		expect(groups).toHaveLength(1);
		expect(groups[0].gallery).toHaveLength(2);
	});
	it('does not override disabled work grouping', () => {
		expect(buildDisplayItems([user, tool, answer], { ...options, groupingEnabled: false }).every(item => item.kind !== 'work_log')).toBe(true);
	});
	it('groups a media-only answer even without routine tools', () => {
		expect(buildDisplayItems([user, answer], options).find(item => item.kind === 'work_log')?.kind).toBe('work_log');
	});
});
