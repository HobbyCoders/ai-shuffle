import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import type { SettingsImportPreview } from '$lib/api/settings-import';
import { platform } from '$lib/stores/platform.svelte';

const mocks = vi.hoisted(() => ({
	preview: vi.fn(),
	apply: vi.fn(),
	loadProjects: vi.fn()
}));

vi.mock('$lib/api/settings-import', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/settings-import')>();
	return {
		...actual,
		previewSettingsImport: mocks.preview,
		applySettingsImport: mocks.apply
	};
});

vi.mock('$lib/stores/app-data.svelte', () => ({
	appData: { loadProjects: mocks.loadProjects }
}));

import SettingsImportDialog from './SettingsImportDialog.svelte';

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
					source_display_path: '/home/appuser/.codex/skills/review',
					destination_display_path: '/home/appuser/.fluid/skills/review',
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
			description: 'Inventory only; unsupported behavior is not activated',
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
		},
		{
			id: 'configured_projects',
			label: 'Configured projects',
			items: [
				{
					id: 'project:missing',
					category_id: 'configured_projects',
					label: 'Codex project: missing',
					provider: 'codex',
					provenance: 'Codex',
					status: 'diagnostic',
					selectable: false,
					selected_by_default: false,
					legacy_detected: true,
					diagnostics: [{ severity: 'info', message: 'Configured project directory does not exist.' }]
				}
			]
		}
	]
};

beforeEach(() => {
	Object.values(mocks).forEach((mock) => mock.mockReset());
	mocks.preview.mockResolvedValue(preview);
	mocks.apply.mockResolvedValue({
		imported: [{ item_id: 'skill:review', label: 'Codex: review skill' }],
		skipped: [],
		conflicts: [],
		errors: [],
		imported_projects: [],
		source_untouched: true,
		overwrite_existing: false
	});
	mocks.loadProjects.mockResolvedValue(undefined);
	platform.set('desktop');
});

afterEach(() => {
	platform.set('desktop');
	cleanup();
});

describe('SettingsImportDialog', () => {
	it('selects providers before preview and exposes paths, provenance, inventory, and diagnostics', async () => {
		const { getByRole, getByText, getAllByText } = render(SettingsImportDialog, {
			props: { onClose: vi.fn() }
		});

		expect(mocks.preview).not.toHaveBeenCalled();
		expect(getByText(/Fluid stays authoritative/i)).toBeTruthy();
		expect(getByText(/MCP details are redacted/i)).toBeTruthy();

		await fireEvent.click(getByRole('radio', { name: /^ClaudeScan/ }));
		await fireEvent.click(getByRole('button', { name: 'Preview settings' }));

		await waitFor(() => expect(getByText('Codex: review skill')).toBeTruthy());
		expect(mocks.preview).toHaveBeenCalledWith({ providers: ['claude'] });
		expect(getByText('/home/appuser')).toBeTruthy();
		expect(getByText('/home/appuser/.fluid')).toBeTruthy();
		expect(getAllByText('Codex').length).toBeGreaterThan(0);
		expect(getByText('Settings and plugins')).toBeTruthy();
		expect(getByText('Configured projects')).toBeTruthy();
		expect(getByText(/Credentials and environment values were redacted/)).toBeTruthy();
		expect(
			(getByRole('checkbox', { name: 'Import Claude: existing skill' }) as HTMLInputElement).disabled
		).toBe(true);
		expect(getByText('Duplicate')).toBeTruthy();
		expect(getByRole('button', { name: /Review import/ })).toBeTruthy();
	});

	it('confirms apply explicitly and refreshes projects after an imported project', async () => {
		mocks.apply.mockResolvedValueOnce({
			imported: [{ item_id: 'skill:review', label: 'Codex: review skill' }],
			skipped: [],
			conflicts: [],
			errors: [],
			imported_projects: ['project-one'],
			source_untouched: true,
			overwrite_existing: false
		});
		const { getByRole, getByText } = render(SettingsImportDialog, {
			props: { onClose: vi.fn() }
		});
		await fireEvent.click(getByRole('button', { name: 'Preview settings' }));
		await waitFor(() => expect(getByText('Codex: review skill')).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: /Review import/ }));

		const applyButton = getByRole('button', { name: 'Import selected' }) as HTMLButtonElement;
		expect(applyButton.disabled).toBe(true);
		await fireEvent.click(
			getByRole('checkbox', {
				name: /provider sources stay untouched, existing Fluid settings win/
			})
		);
		expect(applyButton.disabled).toBe(false);
		await fireEvent.click(applyButton);

		await waitFor(() =>
			expect(mocks.apply).toHaveBeenCalledWith({
				token: 'plan-1',
				selected_item_ids: ['skill:review'],
				providers: ['codex', 'claude']
			})
		);
		await waitFor(() => expect(mocks.loadProjects).toHaveBeenCalledOnce());
		expect(getByText('Import finished')).toBeTruthy();
		expect(getByText(/Projects refreshed/)).toBeTruthy();
	});

	it('returns to source selection when apply reports a stale plan', async () => {
		mocks.apply.mockRejectedValueOnce({ status: 409, detail: 'Preview expired' });
		const { getByRole, getByText } = render(SettingsImportDialog, {
			props: { onClose: vi.fn() }
		});
		await fireEvent.click(getByRole('button', { name: 'Preview settings' }));
		await waitFor(() => expect(getByText('Codex: review skill')).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: /Review import/ }));
		await fireEvent.click(getByRole('checkbox', { name: /I understand/ }));
		await fireEvent.click(getByRole('button', { name: 'Import selected' }));

		await waitFor(() => expect(getByRole('alert').textContent).toContain('preview is out of date'));
		expect(getByRole('button', { name: 'Preview again' })).toBeTruthy();
		expect(getByRole('radio', { name: /Claude \+ Codex/ })).toBeTruthy();
		expect(mocks.loadProjects).not.toHaveBeenCalled();
	});
});
