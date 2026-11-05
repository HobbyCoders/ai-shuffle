import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect, it } from 'vitest';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/ChatInput.svelte'), 'utf8');
const desktop = ":global(html[data-platform='desktop'])";
const rule = (selector: string) => source.split(`${desktop} ${selector} {`)[1]?.split('}')[0] ?? '';

it('does not add text-row height merely by moving the controls below the editor', () => {
	expect(rule('.textarea-wrapper')).toContain('padding: 12px 10px 4px;');
	expect(rule('.chat-island.wrapped .textarea-wrapper')).toContain('padding: 12px 16px 4px;');
});

it('fits the wrapped 32px actions in the same 40px controls row', () => {
	expect(rule('.chat-island.wrapped .composer-attach')).toContain('margin: 4px 0 4px 10px;');
	expect(rule('.chat-island.wrapped .send-cluster')).toContain('margin: 4px 10px 4px 0;');
});
