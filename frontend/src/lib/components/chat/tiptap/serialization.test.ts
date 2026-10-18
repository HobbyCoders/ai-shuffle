import { describe, expect, it } from 'vitest';
import { restoreMessageDocument, serializeToMessage } from './serialization';
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
