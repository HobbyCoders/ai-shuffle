import { describe, expect, it } from 'vitest';
import type { ClusterProject } from '$lib/api/installs';
import {
	buildWorkspacePickerItems,
	pickerItemName,
	selectedPickerId
} from './workspace-catalog';

function catalog(over: Partial<ClusterProject> = {}): ClusterProject {
	return {
		project_v2_id: 'pv2-remote',
		origin: 'git:example.com/other.git',
		name: 'Other',
		local_project_id: null,
		remote_only: true,
		eligible: [
			{
				install_id: 'runner-1',
				device_name: 'Studio',
				online: true,
				is_self: false
			}
		],
		...over
	};
}

describe('buildWorkspacePickerItems', () => {
	it('keeps a local project in the unified list and stamps its projectV2Id', () => {
		const items = buildWorkspacePickerItems(
			[{ id: 'proj-1', name: 'Local' }],
			[catalog({
				project_v2_id: 'pv2-local',
				local_project_id: 'proj-1',
				remote_only: false,
				name: 'Local'
			})]
		);
		expect(items).toEqual([
			{
				id: 'proj-1',
				name: 'Local',
				project: 'proj-1',
				projectV2Id: 'pv2-local',
				remoteOnly: false
			}
		]);
	});

	it('shows projects from online installs directly in the unified list', () => {
		const items = buildWorkspacePickerItems(
			[{ id: 'proj-1', name: 'Local' }],
			[catalog()]
		);
		expect(items).toEqual([
			{
				id: 'proj-1',
				name: 'Local',
				project: 'proj-1',
				projectV2Id: null,
				remoteOnly: false
			},
			{
				id: 'pv2-remote',
				name: 'Other',
				project: '',
				projectV2Id: 'pv2-remote',
				remoteOnly: true
			}
		]);
		expect(items[1]).not.toHaveProperty('path');
	});

	it('does not duplicate a catalog row that already has a local id', () => {
		const items = buildWorkspacePickerItems(
			[{ id: 'proj-1', name: 'Local' }],
			[catalog({
				project_v2_id: 'pv2-local',
				local_project_id: 'proj-1',
				remote_only: false
			})]
		);
		expect(items).toHaveLength(1);
		expect(items[0].id).toBe('proj-1');
	});

	it('merges local clones with the same git origin into one project', () => {
		const items = buildWorkspacePickerItems(
			[
				{ id: 'proj-a', name: 'First clone', link_group_id: 'git:example.com/shared.git' },
				{ id: 'proj-b', name: 'Preferred clone', link_group_id: 'git:example.com/shared.git' }
			],
			[catalog({
				project_v2_id: 'pv2-shared',
				origin: 'git:example.com/shared.git',
				local_project_id: 'proj-b',
				remote_only: false
			})]
		);

		expect(items).toEqual([
			{
				id: 'proj-b',
				name: 'Preferred clone',
				project: 'proj-b',
				projectAliases: ['proj-a'],
				projectV2Id: 'pv2-shared',
				remoteOnly: false
			}
		]);
	});

	it('omits a remote-only project when all eligible installs are offline', () => {
		const items = buildWorkspacePickerItems(
			[],
			[
				catalog({
					eligible: [
						{
							install_id: 'runner-1',
							device_name: 'Studio',
							online: false,
							is_self: false
						}
					]
				})
			]
		);
		expect(items).toEqual([]);
	});
});

describe('selectedPickerId / pickerItemName', () => {
	it('prefers the local project id over the v2 id', () => {
		expect(selectedPickerId('proj-1', 'pv2-1')).toBe('proj-1');
		expect(selectedPickerId('', 'pv2-1')).toBe('pv2-1');
		expect(selectedPickerId(null, null)).toBeNull();
	});

	it('resolves an aliased local id and a v2 id to the merged item', () => {
		const items = buildWorkspacePickerItems(
			[
				{ id: 'proj-a', name: 'First clone', link_group_id: 'git:example.com/shared.git' },
				{ id: 'proj-b', name: 'Preferred clone', link_group_id: 'git:example.com/shared.git' }
			],
			[
				catalog({
					project_v2_id: 'pv2-shared',
					origin: 'git:example.com/shared.git',
					local_project_id: 'proj-b',
					remote_only: false
				})
			]
		);

		expect(selectedPickerId('proj-a', 'pv2-shared', items)).toBe('proj-b');
		expect(selectedPickerId(null, 'pv2-shared', items)).toBe('proj-b');
	});

	it('resolves a remote-only selection name from the unified list', () => {
		const items = buildWorkspacePickerItems([], [catalog({ name: 'Studio repo' })]);
		expect(pickerItemName(items, 'pv2-remote')).toBe('Studio repo');
	});
});
