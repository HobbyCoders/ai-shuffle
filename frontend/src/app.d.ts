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
		}
		// interface Platform {}
	}
}

export {};
