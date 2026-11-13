// See https://kit.svelte.dev/docs/types#app
// for information about these interfaces
declare global {
	/** Release version, injected by Vite from APP_VERSION or package.json. */
	const __APP_VERSION__: string;

	namespace App {
		// interface Error {}
		// interface Locals {}
		// interface PageData {}
		interface PageState {
			/** Set while the phone composer's Chat controls Back entry is on the stack. */
			mobileControls?: boolean;
			/** The Chat controls page (Model, Agent…) whose own Back entry is on the stack. */
			mobileControlsPage?: string;
			mobileDrawer?: boolean;
			/** What Docs showed on this entry: a document (`category/slug`), or '' for the list (see lib/docs/history.ts). */
			docsDoc?: string;
			/** Docs pushed this entry, so the one below it is a Docs entry too. */
			docsStep?: boolean;
		}
		// interface Platform {}
	}
}

export {};
