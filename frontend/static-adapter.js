import adapter from '@sveltejs/adapter-static';
import { copyFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';

const artwork = new URL('./src/lib/components/settings/provider-artwork/', import.meta.url);

// Both UIs compile the provider marks. Keep their notice with the static output
// so worker, desktop and offline-demo packaging can copy the build unchanged.
export function staticAdapter() {
	const options = {
		pages: 'build',
		assets: 'build',
		fallback: 'index.html',
		precompress: false,
		strict: true
	};
	const staticOutput = adapter(options);
	return {
		...staticOutput,
		/** @param {import('@sveltejs/kit').Builder} builder */
		async adapt(builder) {
			await staticOutput.adapt(builder);
			const notice = join(options.assets, 'notices', 'lobehub-provider-artwork');
			await mkdir(notice, { recursive: true });
			for (const file of ['ATTRIBUTION.txt', 'LICENSE']) {
				await copyFile(new URL(file, artwork), join(notice, file));
			}
		}
	};
}
