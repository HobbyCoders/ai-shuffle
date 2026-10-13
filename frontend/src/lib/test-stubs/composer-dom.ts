import { vi } from 'vitest';

// jsdom has no layout observer; Svelte still reads clientHeight at mount.
vi.stubGlobal('ResizeObserver', class {
	observe() {}
	unobserve() {}
	disconnect() {}
});
