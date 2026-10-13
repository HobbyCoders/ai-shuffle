/**
 * Tests for the cluster installs API client.
 *
 * Focused on `getClusterGitState` (W3 / ruling D6): the request path must carry
 * the project id as a properly URL-encoded query param, and the payload must
 * pass through untouched — the composer picker derives device eligibility from
 * it, and a mangled id would silently mark every device ineligible.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
	clusterGitStatePath,
	getClusterGitState,
	listClusterProjects,
	listInstalls,
	type ClusterGitState
} from './installs';

function okJson(body: unknown) {
	return { ok: true, status: 200, json: () => Promise.resolve(body) };
}

function errJson(status: number, body: unknown) {
	return new Response(JSON.stringify(body), { status });
}

const gitState: ClusterGitState = {
	project_v2_id: 'pv2-1',
	installs: [
		{
			id: 'inst-a',
			device_name: 'Desktop',
			base_url: 'http://10.0.0.30:8000',
			platform: 'windows',
			version: '1.2.1',
			capabilities: {},
			last_seen: '2026-08-16T10:00:00Z',
			online: true,
			is_self: true,
			eligible: true,
			checkouts: [
				{
					branch: null,
					path: 'D:/Development/ai-shuffle',
					head_sha: 'abcdef1234567890',
					dirty: false,
					status: 'active',
					updated_at: '2026-08-16T10:00:00Z'
				}
			]
		}
	],
	recent_ops: [
		{
			id: 'op-1',
			runner_id: 'inst-a',
			op: 'ensure_worktree',
			status: 'done',
			params: { branch: 'feat/x' },
			result: {},
			created_at: '2026-08-16T09:59:00Z',
			updated_at: '2026-08-16T10:00:00Z'
		}
	]
};

describe('Installs API', () => {
	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		fetchMock = vi.fn();
		global.fetch = fetchMock as unknown as typeof fetch;
	});

	afterEach(() => {
		vi.restoreAllMocks();
	});

	describe('listInstalls', () => {
		it('GETs the registry endpoint', async () => {
			fetchMock.mockResolvedValueOnce(okJson({ installs: [], self_instance_id: 'inst-a' }));
			const res = await listInstalls();
			expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/cluster/installs');
			expect(fetchMock.mock.calls[0][1].method).toBe('GET');
			expect(res.self_instance_id).toBe('inst-a');
		});
	});

	describe('getClusterGitState', () => {
		it('GETs the git-state endpoint with the project id as a query param', async () => {
			fetchMock.mockResolvedValueOnce(okJson(gitState));
			const res = await getClusterGitState('my-project');
			expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/cluster/git-state?project_id=my-project');
			expect(fetchMock.mock.calls[0][1].method).toBe('GET');
			expect(res).toEqual(gitState);
		});

		it('URL-encodes project ids containing reserved characters', async () => {
			fetchMock.mockResolvedValueOnce(okJson(gitState));
			await getClusterGitState('a b&c/d');
			expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/cluster/git-state?project_id=a%20b%26c%2Fd');
		});

		it('GETs git-state by project_v2_id for a v2-only session', async () => {
			fetchMock.mockResolvedValueOnce(okJson(gitState));
			// Signature is (projectId, projectV2Id) — a v2-only session passes null
			// for the legacy id. This previously passed the id positionally as
			// projectId with the literal 'project_v2_id' as the second argument,
			// which asked for `project_id=pv2-abc` while asserting the opposite.
			await getClusterGitState(null, 'pv2-abc');
			expect(fetchMock.mock.calls[0][0]).toBe(
				'/api/v1/cluster/git-state?project_v2_id=pv2-abc'
			);
		});

		it('throws { detail, status } so callers can fail open on errors', async () => {
			fetchMock.mockResolvedValueOnce(errJson(404, { detail: 'Project not found' }));
			await expect(getClusterGitState('missing')).rejects.toMatchObject({
				detail: 'Project not found',
				status: 404
			});
		});

		it('uses project_v2_id when the local project id is empty', async () => {
			fetchMock.mockResolvedValueOnce(okJson(gitState));
			await getClusterGitState(null, 'pv2-remote');
			expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/cluster/git-state?project_v2_id=pv2-remote');
		});

		it('prefers project_id when both are present', () => {
			expect(clusterGitStatePath('proj-1', 'pv2-1')).toBe(
				'/cluster/git-state?project_id=proj-1'
			);
			expect(clusterGitStatePath('', 'pv2-1')).toBe(
				'/cluster/git-state?project_v2_id=pv2-1'
			);
			expect(clusterGitStatePath(null, null)).toBeNull();
		});
	});

	describe('listClusterProjects', () => {
		it('GETs the catalog endpoint', async () => {
			fetchMock.mockResolvedValueOnce(okJson({ projects: [] }));
			const res = await listClusterProjects();
			expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/cluster/projects');
			expect(res.projects).toEqual([]);
		});
	});
});
