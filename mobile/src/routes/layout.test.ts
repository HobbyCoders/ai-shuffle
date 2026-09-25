import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';

// RootLayout bootstraps worker auth, sync and the API clients; the stub only
// marks whether the page was wrapped in it.
vi.mock('$lib/components/layout/RootLayout.svelte', async () => ({
	default: (await import('../shell/test-fixtures/RootLayoutStub.svelte')).default
}));

import Layout from './+layout.svelte';
import * as appStores from '$app/stores';
import type { Writable } from 'svelte/store';
import { platform } from '$lib/stores/platform.svelte';

// Tests alias $app/stores to a writable stub (vitest.config.ts).
const page = appStores.page as unknown as Writable<unknown>;
const children = createRawSnippet(() => ({ render: () => '<p>page content</p>' }));

function at(path: string) {
	page.set({ url: new URL(`https://appassets.androidplatform.net${path}`), params: {}, data: {}, status: 200, error: null });
	return render(Layout, { props: { children } });
}

afterEach(() => cleanup());

describe('mobile root layout', () => {
	it('wraps the app page in RootLayout in mobile-app mode and selects the mobile shell', () => {
		at('/assets/mobile-ui/');
		const root = screen.getByTestId('root-layout');
		expect(root.getAttribute('data-mobile-app')).toBe('true');
		expect(root.textContent).toContain('page content');
		expect(platform.isMobile).toBe(true);
	});

	it.each(['/assets/mobile-ui/demo', '/assets/mobile-ui/demo/'])(
		'renders the offline demo (%s) without the worker bootstrap',
		(path) => {
			at(path);
			expect(screen.queryByTestId('root-layout')).toBeNull();
			expect(screen.getByText('page content')).toBeTruthy();
		}
	);

	it('does not treat a path that merely contains "demo" as the demo route', () => {
		at('/assets/mobile-ui/demos');
		expect(screen.getByTestId('root-layout')).toBeTruthy();
	});
});
