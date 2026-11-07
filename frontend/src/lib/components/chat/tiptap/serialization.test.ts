import { describe, expect, it } from 'vitest';
import { restoreMessageDocument, serializeToMessage } from './serialization';
const serialize = (text: string, paths: string[]) => {
    const doc = restoreMessageDocument(text, paths);
    return serializeToMessage({ getJSON: () => doc } as Parameters<typeof serializeToMessage>[0]);
};
describe('restored attachment document', () => {
    it('keeps explicit owner identity in a restored chip across draft JSON, without marking a fresh chip', () => {
        const path = '/project/uploads/photo.png';
        const reference = { owner_id: 'owner', event_id: 'sent-event', index: 0, path };
        const restored = restoreMessageDocument('read @' + path, [path], [reference]);
        const chip = JSON.parse(JSON.stringify(restored)).content[0].content[1];
        expect(chip.attrs.uploadReference).toEqual(reference);
        const fresh = restoreMessageDocument('read @' + path, [path]);
        expect(fresh.content![0].content![1].attrs?.uploadReference).toBeUndefined();
    });
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

describe('restored words with a typed upload path (#1084)', () => {
    it('keeps a path that was never attached as text, never a chip', () => {
        const text = 'Explain @/work/uploads/private.txt without opening it';
        const doc = restoreMessageDocument(text);
        expect(JSON.stringify(doc)).not.toContain('fileChip');
        expect(serialize(text, [])).toBe(text);
    });
});
