/**
 * Test stub for SvelteKit's `$app/navigation` virtual module.
 *
 * Provides no-op implementations so stores that call `goto(...)` (e.g. on a
 * 4001 socket close) can be imported in unit tests. Aliased in vitest.config.ts.
 * Tests that care about navigation should mock these explicitly.
 */

export const goto = async (_url?: string | URL): Promise<void> => {};
export const invalidate = async (): Promise<void> => {};
export const invalidateAll = async (): Promise<void> => {};
export const preloadData = async (): Promise<void> => {};
export const preloadCode = async (): Promise<void> => {};
export const beforeNavigate = (): void => {};
export const afterNavigate = (): void => {};
export const onNavigate = (): void => {};
export const disableScrollHandling = (): void => {};
export const pushState = (): void => {};
export const replaceState = (): void => {};
