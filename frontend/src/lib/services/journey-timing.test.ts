import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { beginJourney, cancelJourney, finishJourneyAfterPaint, JOURNEY_SAMPLE_LIMIT } from './journey-timing';
let frames: Map<number, FrameRequestCallback>;
let sequence: number;
const measure = vi.fn();
function frame() {
	const callbacks = [...frames.values()]; frames.clear();
	for (const callback of callbacks) callback(0);
}
beforeEach(() => {
	frames = new Map(); sequence = 0; measure.mockClear();
	Object.defineProperty(document, 'hidden', { configurable: true, value: false });
	vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { frames.set(++sequence, cb); return sequence; });
	vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id));
	vi.stubGlobal('performance', { now: () => 250, clearMeasures: vi.fn(), measure });
});
afterEach(() => { cancelJourney('chat.open', 'a'); cancelJourney('chat.open', 'b'); vi.unstubAllGlobals(); });
it('waits through a rendering opportunity and records no session identifier', () => {
	beginJourney('chat.open', 'a', 100); finishJourneyAfterPaint('chat.open', 'a');
	frame(); expect(measure).not.toHaveBeenCalled(); frame();
	expect(measure).toHaveBeenCalledWith(expect.stringMatching(/^shuffle:chat.open:/), { start: 100, end: 250, detail: { journey: 'chat.open' } });
});
it('ignores old completions after switching and wrong session keys', () => {
	beginJourney('chat.open', 'a'); finishJourneyAfterPaint('chat.open', 'a');
	beginJourney('chat.open', 'b'); finishJourneyAfterPaint('chat.open', 'a');
	frame(); frame(); expect(measure).not.toHaveBeenCalled();
	finishJourneyAfterPaint('chat.open', 'b'); frame(); frame(); expect(measure).toHaveBeenCalledOnce();
});
it('cancels scheduled frames on unmount', () => {
	beginJourney('chat.open', 'a'); const cancel = finishJourneyAfterPaint('chat.open', 'a');
	frame(); cancel(); frame(); expect(measure).not.toHaveBeenCalled();
});
it('excludes hidden samples including a hide/show between frames', () => {
	beginJourney('chat.open', 'a'); finishJourneyAfterPaint('chat.open', 'a'); frame();
	Object.defineProperty(document, 'hidden', { configurable: true, value: true });
	document.dispatchEvent(new Event('visibilitychange'));
	Object.defineProperty(document, 'hidden', { configurable: true, value: false });
	frame(); expect(measure).not.toHaveBeenCalled();
});
it('tracks visibility while awaiting data, before completion is scheduled', () => {
	beginJourney('chat.open', 'a');
	Object.defineProperty(document, 'hidden', { configurable: true, value: true });
	document.dispatchEvent(new Event('visibilitychange'));
	Object.defineProperty(document, 'hidden', { configurable: true, value: false });
	finishJourneyAfterPaint('chat.open', 'a'); frame(); frame();
	expect(measure).not.toHaveBeenCalled();
});
it('offers the navigation origin once per document and never after user activation', async () => {
	vi.resetModules();
	vi.stubGlobal('navigator', { userActivation: { hasBeenActive: false } });
	const fresh = await import('./journey-timing');
	expect(fresh.claimNavigationOrigin()).toBe(0);
	expect(fresh.claimNavigationOrigin()).toBeNull(); // a remount, e.g. after re-login
	vi.resetModules();
	vi.stubGlobal('navigator', { userActivation: { hasBeenActive: true } }); // login form submitted
	expect((await import('./journey-timing')).claimNavigationOrigin()).toBeNull();
	vi.resetModules();
	vi.stubGlobal('navigator', {}); // no User Activation API: keep measuring
	expect((await import('./journey-timing')).claimNavigationOrigin()).toBe(0);
});
it('bounds retained measure names and tolerates unsupported User Timing', () => {
	for (let i = 0; i < 100; i++) {
		beginJourney('chat.open', 'a'); finishJourneyAfterPaint('chat.open', 'a'); frame(); frame();
	}
	expect(new Set(measure.mock.calls.map(([name]) => name)).size).toBe(JOURNEY_SAMPLE_LIMIT);
	measure.mockImplementationOnce(() => { throw new Error('unsupported'); });
	beginJourney('chat.open', 'a'); finishJourneyAfterPaint('chat.open', 'a'); frame(); expect(frame).not.toThrow();
});
