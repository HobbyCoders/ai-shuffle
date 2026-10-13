/**
 * Test stub for SvelteKit's `$app/environment` virtual module.
 *
 * The kit plugin provides this at build/dev time, but vitest runs unit tests
 * without it, so any store importing `{ browser }` from '$app/environment'
 * fails to resolve. Aliased in vitest.config.ts. Tests run in jsdom, so
 * `browser` is true.
 */

export const browser = true;
export const dev = true;
export const building = false;
export const version = 'test';
