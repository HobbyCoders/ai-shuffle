import { afterEach, describe, expect, it, vi } from 'vitest';
import { whenEscapeUnclaimed } from './escape-layers';

const escape = (key = 'Escape') => new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
const afterDispatch = () => new Promise((resolve) => setTimeout(resolve, 0));
const listeners: Array<(e: KeyboardEvent) => void> = [];
function listen(fn: (e: KeyboardEvent) => void) {
	listeners.push(fn);
	window.addEventListener('keydown', fn);
}
afterEach(() => { listeners.splice(0).forEach((fn) => window.removeEventListener('keydown', fn)); });

describe('whenEscapeUnclaimed', () => {
	it('acts once the event has reached every listener and none claimed it', async () => {
		const act = vi.fn();
		listen((e) => whenEscapeUnclaimed(e, act));
		window.dispatchEvent(escape());
		expect(act).not.toHaveBeenCalled();
		await afterDispatch();
		expect(act).toHaveBeenCalledOnce();
	});

	it('stays put when a listener attached AFTER it claims the Escape', async () => {
		const act = vi.fn();
		listen((e) => whenEscapeUnclaimed(e, act));
		listen((e) => e.preventDefault());
		window.dispatchEvent(escape());
		await afterDispatch();
		expect(act).not.toHaveBeenCalled();
	});

	it('stays put when an earlier listener already claimed it', async () => {
		const act = vi.fn();
		listen((e) => e.preventDefault());
		listen((e) => whenEscapeUnclaimed(e, act));
		window.dispatchEvent(escape());
		await afterDispatch();
		expect(act).not.toHaveBeenCalled();
	});

	it('stopPropagation alone does not count as a claim', async () => {
		const act = vi.fn();
		listen((e) => whenEscapeUnclaimed(e, act));
		listen((e) => e.stopPropagation());
		window.dispatchEvent(escape());
		await afterDispatch();
		expect(act).toHaveBeenCalledOnce();
	});

	it('ignores other keys', async () => {
		const act = vi.fn();
		listen((e) => whenEscapeUnclaimed(e, act));
		window.dispatchEvent(escape('Enter'));
		await afterDispatch();
		expect(act).not.toHaveBeenCalled();
	});

	it('can be cancelled before it runs', async () => {
		const act = vi.fn();
		let cancel = () => {};
		listen((e) => { cancel = whenEscapeUnclaimed(e, act); });
		window.dispatchEvent(escape());
		cancel();
		await afterDispatch();
		expect(act).not.toHaveBeenCalled();
	});
});
