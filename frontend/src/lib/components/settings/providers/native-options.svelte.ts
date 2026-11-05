/** Shared enrollment metadata: one request per page, with an explicit retry on failure. */
import { getErrorMessage } from '$lib/api/client';
import { getNativeProviderOptions, type NativeProviderOption } from '$lib/api/model-providers';

export const nativeOptions = $state({
	providers: [] as NativeProviderOption[],
	loaded: false,
	loading: false,
	error: ''
});
let pending: Promise<void> | null = null;
export function loadNativeOptions(): Promise<void> {
	if (nativeOptions.loaded) return Promise.resolve();
	if (pending) return pending;
	nativeOptions.loading = true;
	nativeOptions.error = '';
	pending = Promise.resolve().then(() => getNativeProviderOptions()).then((result) => {
		nativeOptions.providers = result.providers;
		nativeOptions.loaded = true;
	}).catch((error) => {
		nativeOptions.error = getErrorMessage(error, 'Could not load provider options.');
	}).finally(() => {
		nativeOptions.loading = false;
		pending = null;
	});
	return pending;
}

export function nativeOption(id: string, plan: boolean): NativeProviderOption | undefined {
	return nativeOptions.providers.find((p) => p.provider_type === id && (p.auth_mode === 'plan_key') === plan);
}
