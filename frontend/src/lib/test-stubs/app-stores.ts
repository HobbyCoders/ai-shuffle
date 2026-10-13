import { writable } from 'svelte/store';

/** Mutable router state for tests that mount actual SvelteKit route components. */
export const page = writable({ url: new URL('http://localhost/'), params: {}, data: {}, status: 200, error: null });
