/** Tokened page-panel slot. Only the desktop Sidebar opts in as a host. */
import { untrack, type Snippet } from 'svelte';

let panel = $state.raw<Snippet | null>(null);
let owner: symbol | null = null;
const hosts = $state<symbol[]>([]);

export const viewPanel = {
	get panel(): Snippet | null { return panel; },
	get hostMounted(): boolean { return hosts.length > 0; },
	claim(token: symbol, snippet: Snippet): void {
		owner = token;
		panel = snippet;
	},
	release(token: symbol): void {
		if (owner !== token) return;
		owner = null;
		panel = null;
	},
	mountHost(token: symbol): void {
		untrack(() => { if (!hosts.includes(token)) hosts.push(token); });
	},
	unmountHost(token: symbol): void {
		untrack(() => {
			const index = hosts.indexOf(token);
			if (index !== -1) hosts.splice(index, 1);
		});
	}
};
