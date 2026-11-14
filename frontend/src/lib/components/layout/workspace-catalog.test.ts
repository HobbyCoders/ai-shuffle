import { describe, expect, it } from 'vitest';
import type { ClusterProject } from '$lib/api/installs';
import type { Session } from '$lib/api/client';
import { projectIdentity, projectKey } from './session-card-data';
import {
	buildWorkspacePickerItems,
	pickerItemName,
	projectRecency,
	rankPickerItems,
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

describe('the identity the picker and the sidebar Filter share', () => {
	it('gives a project whose catalog row went stale when it was linked the same key as its chats', () => {
		const local = { id: 'proj-1', name: 'Linked', link_group_id: 'git:abc' };
		const items = buildWorkspacePickerItems([local], [
			catalog({ project_v2_id: 'pv2-old', origin: 'local:proj-1', local_project_id: 'proj-1', remote_only: false, name: 'Linked' }),
			catalog({ project_v2_id: 'pv2-new', origin: 'git:abc', local_project_id: null, remote_only: true, name: 'Linked' })
		]);
		expect(items.map((item) => item.id)).toEqual(['proj-1']);
		expect(projectIdentity({ ...local, origin: 'local:proj-1' })).toBe('git:abc');
		expect(projectKey({ id: 's', project_id: 'proj-1', link_group_id: 'git:abc', origin: 'local:proj-1' } as Session)).toBe('git:abc');
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

// The new-session quick picker (Cmd/Ctrl+P): most recently used first, then
// catalog order; a search keeps only matches, best first.
describe('quick picker ordering', () => {
	const items = [
		{ id: 'web', name: 'Website', project: 'web', projectV2Id: null, remoteOnly: false, projectAliases: ['web-old'] },
		{ id: 'api', name: 'API', project: 'api', projectV2Id: null, remoteOnly: false },
		{ id: 'docs', name: 'Docs site', project: 'docs', projectV2Id: null, remoteOnly: false },
		{ id: 'pv2-x', name: 'Infra', project: '', projectV2Id: 'pv2-x', remoteOnly: true }
	];
	const ids = (list: { id: string }[]) => list.map(item => item.id);

	it('takes each project\'s newest session, by its last user message, aliases included', () => {
		const recency = projectRecency(items, [
			{ project_id: 'api', last_user_message_at: '2026-11-01T00:00:00Z', updated_at: '2026-11-09T00:00:00Z' },
			{ project_id: 'web-old', updated_at: '2026-11-05T00:00:00Z' },
			{ project_id: 'docs', updated_at: '2026-10-01T00:00:00Z' },
			{ project_id: 'docs', updated_at: 'not a date' },
			{ project_id: null, updated_at: '2026-11-10T00:00:00Z' },
			{ project_id: 'gone', updated_at: '2026-11-10T00:00:00Z' }
		]);
		expect(Object.fromEntries(recency)).toEqual({
			api: Date.parse('2026-11-01T00:00:00Z'),
			web: Date.parse('2026-11-05T00:00:00Z'),
			docs: Date.parse('2026-10-01T00:00:00Z')
		});
	});

	it('lists recent projects first, then the rest in catalog order', () => {
		const recency = new Map([['docs', 2], ['api', 1]]);
		expect(ids(rankPickerItems(items, '', recency))).toEqual(['docs', 'api', 'web', 'pv2-x']);
		expect(ids(rankPickerItems(items, '   '))).toEqual(['web', 'api', 'docs', 'pv2-x']);
	});

	it('ranks matches: whole name, prefix, word start, inside a word, then letters in order', () => {
		const list = [
			{ id: 'inside', name: 'Mysite' },
			{ id: 'fuzzy', name: 'Some idea test' },
			{ id: 'word', name: 'Docs site' },
			{ id: 'prefix', name: 'Site builder' },
			{ id: 'exact', name: 'SITE' },
			{ id: 'none', name: 'API' }
		];
		expect(ids(rankPickerItems(list, 'Site'))).toEqual(['exact', 'prefix', 'word', 'inside', 'fuzzy']);
	});

	it('breaks ties by recency, then catalog order', () => {
		const recency = new Map([['api', 5]]);
		expect(ids(rankPickerItems(items, 'i', recency))).toEqual(['pv2-x', 'api', 'web', 'docs']);
	});

	it('returns nothing when nothing matches', () => {
		expect(rankPickerItems(items, 'zzz')).toEqual([]);
	});
});
