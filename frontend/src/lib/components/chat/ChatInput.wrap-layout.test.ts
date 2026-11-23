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

// A narrow column on a wide window (a side panel open) is decided from the
// column's own width, not the window's: the controls get a full-width row of
// their own and attach and the circles the row below, so the circles (Stop and
// the stash included) never squeeze the controls until labels run under icons.
it('gives a narrow desktop column a full-width controls row', () => {
	expect(source).toContain('class:composer-narrow={composerNarrow}');
	expect(source).toMatch(/new ResizeObserver\(read\)/);
	const narrowDock = rule('.desktop-composer .composer-narrow .chat-island > :global(.controls-dock)');
	expect(narrowDock).toContain('grid-row: 2;');
	expect(narrowDock).toContain('grid-column: 1 / -1;');
	expect(source).toMatch(/\.composer-narrow \.composer-attach,\s*\.desktop-composer \.composer-narrow \.send-cluster \{\s*grid-row: 3;/);
	// The stash menu opens upward from the circles over the controls row (z 30).
	expect(source).toMatch(/\.desktop-composer \.composer-narrow \.send-cluster \{\s*z-index: 31;/);
	const dock = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/ControlsDock.svelte'), 'utf8');
	const dockRule = (selector: string) => dock.split(`\t${selector} {`)[1]?.split('}')[0] ?? '';
	expect(dockRule(':global(.composer-narrow) .controls-dock')).toContain('flex-wrap: wrap;');
	// Icons never shrink; text labels clip to an ellipsis instead of overlapping.
	expect(dock).toMatch(/\.dock-ico,\s*\.away-badge \{\s*flex: none;/);
	expect(dock).toContain('<span class="dock-txt-label">{permissionLabel}</span>');
});

// Only the phone composers (phone-composer.css, mobile.css) rearrange on wrap.
it('has no rule here that rearranges the composer when its text wraps', () => {
	const style = source.slice(source.indexOf('<style'));
	expect(style).not.toMatch(/\.wrapped\b/);
});
