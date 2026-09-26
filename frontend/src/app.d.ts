// See https://kit.svelte.dev/docs/types#app
// for information about these interfaces
declare global {
	/** Release version, injected by Vite from APP_VERSION or package.json. */
	const __APP_VERSION__: string;

	namespace App {
		// interface Error {}
		// interface Locals {}
		// interface PageData {}
		// interface PageState {}
		// interface Platform {}
	}
}

export {};
