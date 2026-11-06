import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect, it } from 'vitest';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/ChatInput.svelte'), 'utf8');
const rule = (selector: string) => source.split(`\t${selector} {`)[1]?.split('}')[0] ?? '';

// The desktop composer keeps its text on the first row and attach, the controls
// and the send circles on the row below, at every text length. Text that wraps
// therefore never moves the controls, so it cannot add row height or shift Send
// under the pointer.
it('keeps the desktop text and controls on fixed rows', () => {
	expect(rule('.desktop-composer .textarea-wrapper')).toContain('grid-row: 1;');
	expect(rule('.desktop-composer .composer-attach')).toContain('grid-row: 2;');
	expect(rule('.desktop-composer .send-cluster')).toContain('grid-row: 2;');
});

// Only the phone composers (phone-composer.css, mobile.css) rearrange on wrap.
it('has no rule here that rearranges the composer when its text wraps', () => {
	const style = source.slice(source.indexOf('<style'));
	expect(style).not.toMatch(/\.wrapped\b/);
});
