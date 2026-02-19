/**
 * Authentication store — Re-export shim.
 * Implementation lives in auth.svelte.ts (Svelte 5 runes).
 */
export {
	auth,
	isAuthenticated,
	isAdmin,
	setupRequired,
	claudeAuthenticated,
	githubAuthenticated,
	onboardingComplete,
	username,
	authLoading,
	authError,
} from './auth.svelte';
