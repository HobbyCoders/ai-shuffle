import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import FilesPreview from './FilesPreview.svelte';
import type { FileItem } from '$lib/stores/files.svelte';

const file = (name = 'guide.md', size = 100): FileItem => ({ name, path: `docs/${name}`, type: 'file', size, sizeFormatted: '100 B', extension: name.split('.').at(-1)!, modifiedAt: '', createdAt: '' });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('FilesPreview', () => {
	it('reads text from the selected worker and escapes markup instead of executing it', async () => {
		const fetch = vi.fn().mockResolvedValue(new Response('<script>bad()</script>'));
		vi.stubGlobal('fetch', fetch);
		render(FilesPreview, { item: file(), base: '/api/v1/workspace/workers/peer', onClose: vi.fn() });
		expect(await screen.findByText('<script>bad()</script>')).toBeTruthy();
		expect(document.querySelector('.fs-preview script')).toBeNull();
		expect(fetch.mock.calls[0][0]).toBe('/api/v1/workspace/workers/peer/files/download?path=docs%2Fguide.md');
	});

	it.each([['archive.zip', 100], ['large.md', 600_000], ['vector.svg', 100]])('does not fetch unsupported or large files: %s', async (name, size) => {
		const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
		render(FilesPreview, { item: file(name, size), base: '/api/v1/workspace', onClose: vi.fn() });
		expect(screen.getByText(/Preview is not available/)).toBeTruthy();
		expect(fetch).not.toHaveBeenCalled();
	});

	it('offers a retry after a refusal and closes without leaving the folder', async () => {
		const fetch = vi.fn().mockResolvedValueOnce(new Response('', { status: 403 })).mockResolvedValueOnce(new Response('Recovered'));
		vi.stubGlobal('fetch', fetch); const onClose = vi.fn();
		render(FilesPreview, { item: file(), base: '/api/v1/workspace', onClose });
		expect(await screen.findByRole('alert')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
		expect(await screen.findByText('Recovered')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Close preview' }));
		expect(onClose).toHaveBeenCalledOnce();
	});
});
