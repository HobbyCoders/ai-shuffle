/**
 * Store Exports
 *
 * Central export point for all Svelte stores in the application.
 *
 * Note: Some stores have overlapping export names. When importing, prefer
 * importing directly from the specific store module if you need to avoid
 * ambiguity, e.g.: import { deck } from '$lib/stores/deck';
 */

// Auth and User
export * from './auth';

// App State - REMOVED: appState.ts was dead code (replaced by active-session.ts)

// Git integration
export * from './git';

// Chat groups
export * from './groups';

// PWA features
export * from './pwa';

// Theme
export * from './theme';

// Hooks store
export * from './hooks';

// =============================================================================
// Stores with overlapping exports - import directly from module if needed:
// - ./github (githubAuthenticated also in auth)
// =============================================================================
