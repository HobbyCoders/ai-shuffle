// Execute the actual narrow MessageArea adapter; full graph mounting belongs
// to the producing-path journey, where real layout/ResizeObserver behavior runs.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { resolveAnchoredTailReserve } from '$lib/stores/session/auto-scroll';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');
const start = source.indexOf('function handlePlanDisclosure(event: MouseEvent): boolean {');
const end = source.indexOf('\n\t// Wheel/trackpad:', start);
if (start < 0 || end < start) throw new Error('Missing Plan disclosure adapter');
const adapter = source.slice(start, end).replace('(event: MouseEvent): boolean', '(event)');

// Run the actual reserve owner and adapter together, without reimplementing
// their arithmetic. Browser traces cover native layout/clamping between commits.
const tailStart = source.indexOf('function latestTurnTailNow(');
const tailEnd = source.indexOf('\n\tfunction refreshLatestTurnTailReserve()', tailStart);
if (tailStart < 0 || tailEnd < tailStart) throw new Error('Missing passive reserve owner');
const tailOwner = source.slice(tailStart, tailEnd)
 .replace('(pendingRemoval?: HTMLElement): number | null', '(pendingRemoval)');

afterEach(() => document.body.replaceChildren());

function reserveFixture({ earlierTurn = false, clientHeight = 693, contentEnd = 2648.671875,
 removedHeight = 397.796875, initialReserve = 24, controlsExtent = 0 } = {}) {
 const container = document.createElement('div'), anchor = document.createElement('article');
 const button = document.createElement('button'), details = document.createElement('div');
 anchor.id = 'latest-user'; details.id = 'plan-details';
 button.setAttribute('aria-controls', details.id);
 details.style.marginTop = '10px';
 container.append(...(earlierTurn ? [button, details, anchor] : [anchor, button, details]));
 document.body.append(container);
 Object.defineProperty(container, 'scrollTop', { value: 1618, writable: true });
 Object.defineProperty(container, 'clientHeight', { value: clientHeight });
 vi.spyOn(container, 'getBoundingClientRect').mockReturnValue({ top: 52 } as DOMRect);
 vi.spyOn(anchor, 'getBoundingClientRect').mockReturnValue({ top: 66.953125 } as DOMRect);
 vi.spyOn(details, 'getBoundingClientRect').mockReturnValue({ height: removedHeight } as DOMRect);
 const scope = {
  $activeSession: { sessionId: 'presented' }, $liveActiveSession: { sessionId: 'presented' },
  transcriptActions: { isCurrent: () => true }, containerRef: container,
  tailSpacerRef: document.createElement('div'), currentTurnAnchorId: null as string | null,
  latestTurnTailPx: initialReserve, transcript: { latestUserTurnId: 'latest-user' },
  MIN_TRANSCRIPT_TAIL_RESERVE: 24, turnAnchorId: (id: string) => id,
  transcriptContentEnd: () => contentEnd, readingFloorReserve: () => 0,
  tailControlsExtent: () => controlsExtent, resolveAnchoredTailReserve,
  disengageAutoScroll: vi.fn()
 };
 const owner = new Function('scope', `with(scope) { ${tailOwner}; ${adapter}; return { latestTurnTailNow, handlePlanDisclosure }; }`)(scope);
 button.addEventListener('click', event => owner.handlePlanDisclosure(event));
 return { scope, details, owner, click: () => button.click() };
}

describe('Plan collapse passive-reserve handoff', () => {
 it('pre-grows 24 to 70 before the historical details are removed', () => {
  const f = reserveFixture();
  expect(f.owner.latestTurnTailNow()).toBe(24);
  f.click();
  expect(f.scope.disengageAutoScroll).toHaveBeenCalledOnce();
  expect(f.scope.latestTurnTailPx).toBe(70);
  expect(f.details.isConnected).toBe(true);
  // The same DOM commit can now shrink directly to its final 2311px extent,
  // never visiting the native-clamping intermediate 2265px extent.
 });
 it('pre-grows current-card room to 133, leaving only its real physical clamp', () => {
  const f = reserveFixture({ clientHeight: 593, contentEnd: 2240.875, removedHeight: 153.890625 });
  f.click();
  expect(f.scope.latestTurnTailPx).toBe(133);
 });
 it('does not project a removal before the latest user anchor', () => {
  const f = reserveFixture({ earlierTurn: true });
  expect(f.owner.latestTurnTailNow(f.details)).toBe(f.owner.latestTurnTailNow());
  f.click();
  expect(f.scope.latestTurnTailPx).toBe(24);
 });
 it('never shrinks a larger handed-off reserve before removal', () => {
  const f = reserveFixture({ initialReserve: 120 });
  f.click();
  expect(f.scope.latestTurnTailPx).toBe(120);
 });
 it('leaves ordinary refresh, controlled-turn and tail-control semantics intact', () => {
  const f = reserveFixture({ contentEnd: 2240.875, controlsExtent: 40 });
  expect(f.owner.latestTurnTailNow()).toBe(110);
  f.scope.currentTurnAnchorId = 'controlled';
  expect(f.owner.latestTurnTailNow(f.details)).toBeNull();
 });
 it('does not project an expansion or detached details from another transcript', () => {
  const f = reserveFixture();
  document.body.append(f.details);
  f.click();
  expect(f.scope.latestTurnTailPx).toBe(24);
 });
});

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
