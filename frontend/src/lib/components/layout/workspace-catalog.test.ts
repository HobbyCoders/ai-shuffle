import { describe, expect, it } from 'vitest';
import type { ClusterProject } from '$lib/api/installs';
import {
	ON_OTHER_RUNNERS,
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
		eligible: [],
		...over
	};
}

describe('buildWorkspacePickerItems', () => {
	it('keeps local projects on the main list and stamps projectV2Id from the catalog', () => {
		const { items, remoteItems } = buildWorkspacePickerItems(
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
		expect(remoteItems).toEqual([]);
	});

	it('groups remote-only catalog rows under "On other runners" without a path', () => {
		const { items, remoteItems } = buildWorkspacePickerItems(
			[{ id: 'proj-1', name: 'Local' }],
			[catalog()]
		);
		expect(items).toHaveLength(1);
		expect(remoteItems).toEqual([
			{
				id: 'pv2-remote',
				name: 'Other',
				project: '',
				projectV2Id: 'pv2-remote',
				remoteOnly: true
			}
		]);
		expect(ON_OTHER_RUNNERS).toBe('On other runners');
		expect(remoteItems[0]).not.toHaveProperty('path');
	});

	it('does not duplicate a catalog row that already has a local id', () => {
		const { remoteItems } = buildWorkspacePickerItems(
			[{ id: 'proj-1', name: 'Local' }],
			[catalog({
				project_v2_id: 'pv2-local',
				local_project_id: 'proj-1',
				remote_only: false
			})]
		);
		expect(remoteItems).toEqual([]);
	});
});

describe('selectedPickerId / pickerItemName', () => {
	it('prefers the local project id over the v2 id', () => {
		expect(selectedPickerId('proj-1', 'pv2-1')).toBe('proj-1');
		expect(selectedPickerId('', 'pv2-1')).toBe('pv2-1');
		expect(selectedPickerId(null, null)).toBeNull();
	});

	it('resolves a remote-only selection name from the remote group', () => {
		const { items, remoteItems } = buildWorkspacePickerItems(
			[],
			[catalog({ name: 'Studio repo' })]
		);
		expect(pickerItemName(items, remoteItems, 'pv2-remote')).toBe('Studio repo');
	});
});
