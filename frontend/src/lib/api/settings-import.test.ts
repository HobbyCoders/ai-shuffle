import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { applySettingsImport, previewSettingsImport } from './settings-import';

function response(body: unknown, status = 200): Response {
	return {
		ok: status >= 200 && status < 300,
		status,
		json: vi.fn().mockResolvedValue(body)
	} as unknown as Response;
}

describe('provider settings import API', () => {
	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
	});

	afterEach(() => vi.unstubAllGlobals());

	it('previews both providers through the provider-neutral route', async () => {
		fetchMock.mockResolvedValueOnce(
			response({
				token: 'plan-1',
				hash: 'sha256:abc',
				providers: ['codex', 'claude'],
				source_display_path: '/home/appuser',
				destination_display_path: '/home/appuser/.fluid',
				source_untouched: true,
				overwrite_existing: false,
				legacy_detected: true,
				categories: [
					{
						id: 'skills',
						label: 'Skills',
						items: [
							{
								id: 'skill:review',
								label: 'Codex: review',
								source: '/home/appuser/.codex/skills/review',
								destination: '/home/appuser/.fluid/skills/review',
								status: 'ready'
							}
						]
					}
				],
				conflicts: [],
				diagnostics: []
			})
		);

		const result = await previewSettingsImport({ source_home: '   ' });

		expect(fetchMock).toHaveBeenCalledWith('/api/settings/import/preview', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			redirect: 'error', credentials: 'include',
			body: JSON.stringify({ providers: ['codex', 'claude'] })
		});
		expect(result.providers).toEqual(['codex', 'claude']);
		expect(result.categories[0].items[0]).toMatchObject({
			id: 'skill:review',
			provider: 'codex',
			provenance: 'Codex',
			selectable: true,
			selected_by_default: true,
			source_display_path: '/home/appuser/.codex/skills/review',
			destination_display_path: '/home/appuser/.fluid/skills/review'
		});
	});

	it('sends the selected provider and plan identity when applying', async () => {
		fetchMock.mockResolvedValueOnce(
			response({
				imported: [{ item_id: 'project:one', label: 'Codex project: one' }],
				skipped: [],
				conflicts: [],
				errors: [],
				imported_projects: [
					{ project_id: 'project-one', path: '/workspace/one', status: 'imported' },
					{ project_id: 'existing', path: '/workspace/existing', status: 'conflict' }
				],
				source_untouched: true,
				overwrite_existing: false
			})
		);

		const result = await applySettingsImport({
			token: 'plan-1',
			selected_item_ids: ['project:one'],
			providers: ['codex'],
			source_home: ' /imports/providers '
		});

		expect(fetchMock).toHaveBeenCalledWith('/api/settings/import/apply', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			redirect: 'error', credentials: 'include',
			body: JSON.stringify({
				token: 'plan-1',
				selected_item_ids: ['project:one'],
				providers: ['codex'],
				source_home: '/imports/providers'
			})
		});
		expect(result.imported_projects).toEqual(['project-one']);
		expect(result.overwrite_existing).toBe(false);
	});

	it('preserves duplicate, conflict, and diagnostic states for the preview UI', async () => {
		fetchMock.mockResolvedValueOnce(
			response({
				token: 'plan-2',
				categories: {
					settings_plugins_inventory: {
						items: [
							{ id: 'mcp:one', label: 'Claude: settings.json', status: 'diagnostic', message: 'MCP values redacted' },
							{ id: 'duplicate:one', label: 'Claude: AGENTS.md', status: 'duplicate' }
						]
					}
				}
			})
		);

		const result = await previewSettingsImport({ providers: ['claude'] });
		const [diagnostic, duplicate] = result.categories[0].items;

		expect(diagnostic).toMatchObject({ provider: 'claude', status: 'diagnostic', selectable: false });
		expect(duplicate).toMatchObject({ provider: 'claude', status: 'duplicate', selectable: false });
	});

	it('preserves a stale-plan 409 for the dialog to recover from', async () => {
		fetchMock.mockResolvedValueOnce(response({ detail: 'Preview expired' }, 409));

		await expect(
			applySettingsImport({
				token: 'expired',
				selected_item_ids: ['skill:review'],
				providers: ['claude']
			})
		).rejects.toEqual({ status: 409, detail: 'Preview expired' });
	});
});
