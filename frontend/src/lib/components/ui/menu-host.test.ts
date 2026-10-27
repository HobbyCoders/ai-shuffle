import { describe, expect, it } from 'vitest';
import { menuHost } from './menu-host';

describe('menuHost', () => {
	it('leaves the node in place without a host', () => {
		const home = document.createElement('div');
		const node = document.createElement('div');
		home.appendChild(node);
		menuHost(node, null);
		expect(node.parentNode).toBe(home);
	});

	it('moves the node into the host, and into a new host on update', () => {
		const home = document.createElement('div');
		const host = document.createElement('section');
		const next = document.createElement('section');
		const node = document.createElement('div');
		home.appendChild(node);
		const action = menuHost(node, host);
		expect(node.parentNode).toBe(host);
		action.update(next);
		expect(node.parentNode).toBe(next);
		// A null update keeps it where it is; the owner removes it on close.
		action.update(null);
		expect(node.parentNode).toBe(next);
	});

	it('removes a moved node when it is destroyed, so an ancestor re-render leaves nothing behind', () => {
		const home = document.createElement('div');
		const host = document.createElement('section');
		const node = document.createElement('div');
		home.appendChild(node);
		const action = menuHost(node, host);
		action.destroy();
		expect(host.childElementCount).toBe(0);
		// An unmoved node is left to its owner.
		const still = document.createElement('div');
		home.appendChild(still);
		menuHost(still, null).destroy();
		expect(still.parentNode).toBe(home);
	});
});
