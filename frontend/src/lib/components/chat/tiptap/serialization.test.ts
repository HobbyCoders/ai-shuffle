import { afterEach, describe, expect, it } from 'vitest';
import { Editor, type JSONContent } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import { FileChipNode } from './FileChipNode';
import { extractFileChips, isUploadChip, joinDrafts, restoreMessageDocument, serializeToMessage } from './serialization';
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
describe('a chip keeps what it is through the real editor', () => {
    const attrs = (fullPath: string, extra: object = {}) => ({ path: fullPath, filename: fullPath.split(/[/\\]/).pop()!, fullPath, size: 0, type: 'file' as const, ...extra });
    const docWith = (chipAttrs: object): JSONContent => ({ type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'fileChip', attrs: chipAttrs }] }] });
    const editors: Editor[] = [];
    const open = (content: JSONContent | string = '') => {
        const editor = new Editor({ extensions: [StarterKit, FileChipNode], content });
        editors.push(editor);
        return editor;
    };
    const uploads = (editor: Editor) => extractFileChips(editor).filter(isUploadChip).map(c => c.fullPath);
    const picked = (fullPath: string) => {
        const editor = open();
        editor.commands.insertFileChip(attrs(fullPath, { mention: true }));
        return editor;
    };
    afterEach(() => editors.splice(0).forEach(editor => editor.destroy()));

    it.each(['C:/notes.txt', 'C:\\notes.txt', '/etc/hosts', '\\\\host\\share\\a.txt', 'README.md'])(
        'never uploads a chip picked from the @ menu: %s', (path) => {
            expect(uploads(picked(path))).toEqual([]);
        });

    it('still sends the picked path as text', () => {
        expect(serializeToMessage(picked('C:/notes.txt'))).toBe('@C:/notes.txt');
    });

    it('stays a mention after a draft round trip, joined drafts and a copy within the composer', () => {
        const editor = picked('C:/notes.txt');
        const draft = JSON.stringify(editor.getJSON());
        expect(uploads(open(JSON.parse(draft)))).toEqual([]);
        const joined = open(JSON.parse(joinDrafts(draft, draft)));
        expect(extractFileChips(joined)).toHaveLength(2);
        expect(uploads(joined)).toEqual([]);
        const copied = open(editor.getHTML());
        expect(extractFileChips(copied)).toHaveLength(1);
        expect(uploads(copied)).toEqual([]);
    });

    it.each(['C:\\work\\uploads\\photo.png', 'C:/work/uploads/photo.png', '\\\\server\\share\\photo.png', '/workspace/app/uploads/photo.png'])(
        'delivers a genuine upload through the same round trip: %s', (path) => {
            const editor = open();
            editor.commands.insertFileChip(attrs(path));
            expect(uploads(editor)).toEqual([path]);
            expect(uploads(open(JSON.parse(JSON.stringify(editor.getJSON()))))).toEqual([path]);
            expect(uploads(open(editor.getHTML()))).toEqual([path]);
        });

    it('falls back to the path for a draft saved before the mark existed', () => {
        expect(uploads(open(docWith(attrs('README.md'))))).toEqual([]);
        expect(uploads(open(docWith(attrs('/workspace/app/uploads/old.png'))))).toEqual(['/workspace/app/uploads/old.png']);
        expect(uploads(open(docWith(attrs('C:/work/uploads/old.png'))))).toEqual(['C:/work/uploads/old.png']);
    });
});

describe('telling uploads from @ mentions', () => {
    const chip = (type: 'file' | 'chat_history' | 'note' | 'subagent', fullPath: string) => ({ type, fullPath });

    it.each([
        ['a project upload', '/workspace/app/uploads/photo.png'],
        ['a chat upload', '/data/workspace/.chat/uploads/notes.txt'],
        ['a Windows upload', 'C:\\work\\app\\uploads\\photo.png'],
        ['a Windows upload with forward slashes', 'C:/work/app/uploads/photo.png'],
        ['an upload on a network share', '\\\\server\\share\\photo.png'],
    ])('keeps %s', (_name, path) => {
        expect(isUploadChip(chip('file', path))).toBe(true);
    });

    it.each([
        ['a project file', chip('file', 'README.md')],
        ['a nested project file', chip('file', 'src/lib/api.ts')],
        ['a file in the uploads folder picked by name', chip('file', 'uploads/photo.png')],
        ['a chat history entry', chip('chat_history', 'Chat History/session.jsonl')],
        ['a note at an absolute path', chip('note', '/data/notes/plan.md')],
        ['a note without a file', chip('note', 'Notes/abc')],
        ['a subagent', chip('subagent', 'Subagents/reviewer')],
        ['an empty chip', chip('file', '')],
    ])('leaves out %s', (_name, attrs) => {
        expect(isUploadChip(attrs)).toBe(false);
    });

    it('treats a chip restored from a sent upload as an upload, with or without its reference', () => {
        const path = '/workspace/app/uploads/photo.png';
        const reference = { owner_id: 'owner', event_id: 'sent-event', index: 0, path };
        for (const references of [[], [reference]]) {
            const restored = restoreMessageDocument(`read @${path}`, [path], references);
            const attrs = JSON.parse(JSON.stringify(restored)).content[0].content[1].attrs;
            expect(isUploadChip(attrs)).toBe(true);
        }
    });

    it('leaves a mention in restored text as text, beside a restored upload', () => {
        const path = '/workspace/app/uploads/photo.png';
        const restored = restoreMessageDocument(`see @README.md and @${path}`, [path]);
        expect(restored.content?.[0].content?.map(node => node.type)).toEqual(['text', 'fileChip']);
        expect(serialize(`see @README.md and @${path}`, [path])).toBe(`see @README.md and @${path}`);
    });
});
