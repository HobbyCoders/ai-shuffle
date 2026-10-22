import { loadHtmlPreview, retryHtmlPreview } from '$lib/utils/htmlPreview';

/**
 * The loaded source of one HTML artifact, for the component that creates it
 * (must be constructed during component init). Loads through the
 * authenticated, size-bounded loader, keeps a download URL for the exact
 * bytes shown, and reloads on demand.
 */
export class HtmlArtifactSource {
	source = $state('');
	loading = $state(true);
	error = $state('');
	revision = $state(0);
	downloadUrl = $state('');
	#target: () => { href: string; sessionId?: string | null };

	constructor(target: () => { href: string; sessionId?: string | null }) {
		this.#target = target;
		// Store-backed prop getters can track a whole session object (draft,
		// scroll). Scalar deriveds keep unchanged identities from reloading.
		const href = $derived(target().href);
		const sessionId = $derived(target().sessionId ?? null);

		$effect(() => {
			const url = href;
			const owner = sessionId;
			void this.revision;
			const controller = new AbortController();
			this.loading = true;
			this.error = '';
			this.source = '';
			loadHtmlPreview(url, controller.signal, owner).then((html) => {
				if (!controller.signal.aborted) { this.source = html; this.loading = false; }
			}).catch((reason: unknown) => {
				if (!controller.signal.aborted) {
					this.error = reason instanceof Error ? reason.message : 'Could not load this HTML file.';
					this.loading = false;
				}
			});
			return () => controller.abort();
		});

		$effect(() => {
			const source = this.source;
			if (!source) { this.downloadUrl = ''; return; }
			const url = URL.createObjectURL(new Blob([source], { type: 'text/html;charset=utf-8' }));
			this.downloadUrl = url;
			return () => URL.revokeObjectURL(url);
		});
	}

	/** Forget a remembered 404 and fetch again. */
	reload = () => {
		const { href, sessionId } = this.#target();
		retryHtmlPreview(href, sessionId);
		this.revision += 1;
	};
}
