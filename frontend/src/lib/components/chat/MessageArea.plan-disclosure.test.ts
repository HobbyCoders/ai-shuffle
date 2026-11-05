// Execute the actual narrow MessageArea adapter; full graph mounting belongs
// to the producing-path journey, where real layout/ResizeObserver behavior runs.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it, vi } from 'vitest';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');
const start = source.indexOf('function handlePlanDisclosure(event: MouseEvent): boolean {');
const end = source.indexOf('\n\t// Wheel/trackpad:', start);
if (start < 0 || end < start) throw new Error('Missing Plan disclosure adapter');
const adapter = source.slice(start, end).replace('(event: MouseEvent): boolean', '(event)');

function fixture({ current = true, sameSession = true, contained = true } = {}) {
 const release = vi.fn();
 const container = document.createElement('div');
 const button = document.createElement('button');
 if (contained) container.append(button);
 const actions = { isCurrent: vi.fn(() => current) };
 const handle = new Function('$activeSession', '$liveActiveSession', 'transcriptActions', 'containerRef', 'disengageAutoScroll', `${adapter}; return handlePlanDisclosure;`)(
  { sessionId: 'presented', isRemote: true }, { sessionId: sameSession ? 'presented' : 'entered' }, actions, container, release
 );
 let accepted: boolean | undefined;
 button.addEventListener('click', event => { accepted = handle(event); });
 button.click();
 return { release, accepted, actions };
}

describe('MessageArea Plan reader intent', () => {
 it('releases following for the active read-only mirrored transcript too', () => {
  const { release, accepted, actions } = fixture();
  expect(accepted).toBe(true);
  expect(release).toHaveBeenCalledOnce();
  expect(actions.isCurrent).toHaveBeenCalledWith('presented');
 });
 it.each([
  ['retained outgoing surface', { current: false }],
  ['session already switched', { sameSession: false }],
  ['button outside this transcript', { contained: false }]
 ])('vetoes a disclosure from a %s without releasing the entered reader', (_label, options) => {
  const { release, accepted } = fixture(options);
  expect(accepted).toBe(false);
  expect(release).not.toHaveBeenCalled();
 });
 it('wires intent only to Plan disclosure, not every transcript click or arrival', () => {
  expect(source).toContain('onBeforeToggle={handlePlanDisclosure}');
  expect(source.match(/onBeforeToggle=/g)).toHaveLength(1);
  expect(adapter).not.toMatch(/scrollTop\s*=|scrollToBottom|ResizeObserver|setTimeout/);
 });
});
