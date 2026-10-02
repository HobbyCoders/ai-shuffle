import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { SettingsImportPreview } from '$lib/api/settings-import';

const mocks = vi.hoisted(() => ({ preview: vi.fn(), apply: vi.fn(), loadProjects: vi.fn() }));

vi.mock('$lib/api/settings-import', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/settings-import')>();
	return { ...actual, previewSettingsImport: mocks.preview, applySettingsImport: mocks.apply };
});
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { loadProjects: mocks.loadProjects } }));

import ImportWizard from './ImportWizard.svelte';

const preview: SettingsImportPreview = {
	token: 'plan-1',
	hash: 'sha256:abc',
	providers: ['codex', 'claude'],
	source_display_path: '/home/appuser',
	destination_display_path: '/home/appuser/.fluid',
	legacy_detected: true,
	source_untouched: true,
	overwrite_existing: false,
	conflicts: ['Claude agent review already maps to a Codex-native destination.'],
	diagnostics: [{ severity: 'info', message: 'Secret values were excluded.' }],
	categories: [
		{
			id: 'skills',
			label: 'Skills',
			items: [
				{
					id: 'skill:review',
					category_id: 'skills',
					label: 'Codex: review skill',
					provider: 'codex',
					provenance: 'Codex',
					status: 'ready',
					selectable: true,
					selected_by_default: true,
					legacy_detected: true,
					diagnostics: []
				},
				{
					id: 'skill:existing',
					category_id: 'skills',
					label: 'Claude: existing skill',
					provider: 'claude',
					provenance: 'Claude',
					status: 'duplicate',
					selectable: false,
					selected_by_default: false,
					legacy_detected: true,
					conflict: 'Destination file already exists.',
					diagnostics: []
				}
			]
		},
		{
			id: 'settings_plugins_inventory',
			label: 'Settings and plugins',
			description: 'Hooks, enabled Claude plugins and Codex model/approval settings are copied; other settings are listed only',
			items: [
				{
					id: 'inventory:mcp',
					category_id: 'settings_plugins_inventory',
					label: 'Claude: MCP server inventory',
					provider: 'claude',
					provenance: 'Claude',
					status: 'diagnostic',
					selectable: false,
					selected_by_default: false,
					legacy_detected: true,
					diagnostics: [{ severity: 'info', message: 'Credentials and environment values were redacted.' }]
				}
			]
		}
	]
};

const applied = (projects: string[] = []) => ({
	imported: [{ item_id: 'skill:review', label: 'Codex: review skill' }],
	skipped: [],
	conflicts: [],
	errors: [],
	imported_projects: projects,
	source_untouched: true,
	overwrite_existing: false
});

beforeEach(() => {
	Object.values(mocks).forEach((mock) => mock.mockReset());
	mocks.preview.mockResolvedValue(preview);
	mocks.apply.mockResolvedValue(applied());
	mocks.loadProjects.mockResolvedValue(undefined);
});
afterEach(cleanup);

async function scanned() {
	await fireEvent.click(screen.getByRole('button', { name: 'Scan' }));
	await screen.findByText('Skills');
}

describe('ImportWizard', () => {
	it('chooses the source before scanning and shows what was found, with locked duplicates', async () => {
		render(ImportWizard);
		expect(mocks.preview).not.toHaveBeenCalled();
		expect(screen.getByText(/MCP secrets are never shown or copied/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Claude' }));
		expect(screen.getByText('~/.claude')).toBeTruthy();
		await scanned();
		expect(mocks.preview).toHaveBeenCalledWith({ providers: ['claude'] });
		expect(screen.getByText('/home/appuser')).toBeTruthy();
		expect(screen.getByText('1 of 2')).toBeTruthy();
		expect(screen.getByText(/kept as they are/)).toBeTruthy();
		expect(screen.getByText('Secret values were excluded.')).toBeTruthy();

		await fireEvent.click(screen.getByRole('button', { name: 'Show Skills items' }));
		expect((screen.getByRole('checkbox', { name: 'Import Claude: existing skill' }) as HTMLInputElement).disabled).toBe(true);
		expect(screen.getByText('Duplicate')).toBeTruthy();
		expect(screen.getByText('Destination file already exists.')).toBeTruthy();

		await fireEvent.click(screen.getByRole('button', { name: 'Show Settings and plugins items' }));
		expect(screen.getByText(/Hooks, enabled Claude plugins and Codex model\/approval settings are copied/)).toBeTruthy();
		expect(screen.getByText(/Credentials and environment values were redacted/)).toBeTruthy();
		expect((screen.getByRole('checkbox', { name: 'Select all Settings and plugins' }) as HTMLInputElement).disabled).toBe(true);
	});

	it('imports only after the acknowledgement and refreshes projects after an imported project', async () => {
		mocks.apply.mockResolvedValueOnce(applied(['project-one']));
		const onImported = vi.fn();
		render(ImportWizard, { onImported });
		await scanned();
		const importButton = screen.getByRole('button', { name: 'Import 1 item' }) as HTMLButtonElement;
		expect(importButton.disabled).toBe(true);
		await fireEvent.click(screen.getByRole('checkbox', { name: 'Sources untouched, existing settings win' }));
		expect(importButton.disabled).toBe(false);
		await fireEvent.click(importButton);
		await waitFor(() =>
			expect(mocks.apply).toHaveBeenCalledWith({ token: 'plan-1', selected_item_ids: ['skill:review'], providers: ['codex', 'claude'] })
		);
		await waitFor(() => expect(mocks.loadProjects).toHaveBeenCalledOnce());
		expect(screen.getByText('Imported 1')).toBeTruthy();
		expect(screen.getByText('Projects refreshed')).toBeTruthy();
		expect(onImported).toHaveBeenCalledWith(1);
	});

	it('deselecting a category clears the acknowledgement and the count', async () => {
		render(ImportWizard);
		await scanned();
		await fireEvent.click(screen.getByRole('checkbox', { name: 'Sources untouched, existing settings win' }));
		await fireEvent.click(screen.getByRole('checkbox', { name: 'Select all Skills' }));
		expect(screen.getByText('0 of 2')).toBeTruthy();
		expect((screen.getByRole('button', { name: 'Import 0 items' }) as HTMLButtonElement).disabled).toBe(true);
		expect((screen.getByRole('checkbox', { name: 'Sources untouched, existing settings win' }) as HTMLInputElement).checked).toBe(false);
	});

	it('returns to the source when the preview went stale', async () => {
		mocks.apply.mockRejectedValueOnce({ status: 409, detail: 'Preview expired' });
		render(ImportWizard);
		await scanned();
		await fireEvent.click(screen.getByRole('checkbox', { name: 'Sources untouched, existing settings win' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Import 1 item' }));
		await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('preview is out of date'));
		expect(screen.getByRole('button', { name: 'Scan again' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Claude and Codex' })).toBeTruthy();
		expect(mocks.loadProjects).not.toHaveBeenCalled();
	});

	it('reports a scan that failed and stays on the source', async () => {
		mocks.preview.mockRejectedValueOnce({ status: 400, detail: 'Settings home not found' });
		render(ImportWizard);
		await fireEvent.click(screen.getByRole('button', { name: 'Scan' }));
		expect((await screen.findByRole('alert')).textContent).toBe('Settings home not found');
		expect(screen.getByRole('button', { name: 'Scan' })).toBeTruthy();
	});
});
