// See https://svelte.dev/docs/kit/types#app.d.ts
// for information about these interfaces
declare global {
	// Replaced at build time (vite.config.js `define`, shared with ../frontend).
	const __APP_VERSION__: string;

	namespace App {
		// interface Error {}
		// interface Locals {}
		// interface PageData {}
		interface PageState {
			/** Set while the navigation drawer's Back entry is on the stack. */
			mobileDrawer?: boolean;
			/** Set while the chat controls panel's Back entry is on the stack. */
			mobileControls?: boolean;
			/** The Chat controls page (Model, Agent…) whose own Back entry is on the stack. */
			mobileControlsPage?: string;
		}
		// interface Platform {}
	}
}

export {};
