import { describe, expect, it } from 'vitest';
import { attachmentMentions, restoreMessageDocument, serializeToMessage } from './serialization';
const serialize = (text: string, paths: string[]) => {
    const doc = restoreMessageDocument(text, paths);
    return serializeToMessage({ getJSON: () => doc } as Parameters<typeof serializeToMessage>[0]);
};
describe('restored attachment document', () => {
    it.each([
        ['see @/uploads/proof.png please', ['/uploads/proof.png']],
        ['@/uploads/proof.png\n@/uploads/proof.png again', ['/uploads/proof.png']],
        ['@/uploads/proof.png @/uploads/proof', ['/uploads/proof', '/uploads/proof.png']],
    ] as [string, string[]][])('round-trips serialized text in place: %s', (text, paths) => {
        expect(serialize(text, paths)).toBe(text);
    });
    it('adds only missing paths and deduplicates the supplied attachment list', () => {
        expect(serialize('see @/a.png please', ['/a.png','/b.png','/b.png'])).toBe('see @/a.png please @/b.png');
        expect(serialize('', ['/a.png'])).toBe('@/a.png');
    });
});

describe('attachmentMentions (#1084)', () => {
	const session = '/data/workspace/session-uploads/' + 'a'.repeat(64) + '/notes.txt';
	it('reads the uploads a sent prompt carried, in order, once each', () => {
		const text = `look at @${session} and @/srv/app/uploads/plan.md, then @${session} again`;
		expect(attachmentMentions(text)).toEqual([session, '/srv/app/uploads/plan.md']);
	});
	it('reads a chip dropped straight after a word, with no space before it', () => {
		expect(attachmentMentions('look at this@/srv/app/uploads/plan.md')).toEqual(['/srv/app/uploads/plan.md']);
	});
	it('reads Windows project uploads too', () => {
		expect(attachmentMentions('see @C:\\code\\proj\\uploads\\a.txt')).toEqual(['C:\\code\\proj\\uploads\\a.txt']);
	});
	it('ignores ordinary file mentions, emails and paths that are not uploads', () => {
		expect(attachmentMentions('fix @src/lib/api.ts, mail me@x.io, see @/srv/app/uploads-old/a.txt')).toEqual([]);
	});
	it('turns them back into chips with the words around them', () => {
		const text = `words @${session} more`;
		const doc = restoreMessageDocument(text, attachmentMentions(text));
		expect(doc.content?.[0].content?.map((n) => n.type)).toEqual(['text', 'fileChip', 'text']);
		expect(JSON.stringify(doc)).not.toContain('@/data');
	});
});
