// #1636 review: a new chat's remembered project and branch resolve against
// both halves of the project catalog, and the composer holds a send until both
// have answered. This pins that readiness signal.
import { beforeEach, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), listClusterProjects: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, post: vi.fn(), put: vi.fn(), delete: vi.fn() },
	searchSessionsPaginated: vi.fn(), renameSession: vi.fn(), setSessionActive: vi.fn(),
}));
vi.mock('$lib/api/installs', () => ({ listClusterProjects: mocks.listClusterProjects }));
vi.mock('./active-session', () => ({ activeSession: { current: { sessionId: null } } }));

let appData: (typeof import('./app-data.svelte'))['appData'];
beforeEach(async () => {
	vi.resetModules(); vi.clearAllMocks();
	appData = (await import('./app-data.svelte')).appData;
});
const deferred = <T>() => {
	let resolve!: (value: T) => void, reject!: (error: unknown) => void;
	const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
	return { promise, resolve, reject };
};

it('is ready only once both catalog halves have answered', async () => {
	const local = deferred<unknown>(), cluster = deferred<unknown>();
	mocks.get.mockReturnValue(local.promise); mocks.listClusterProjects.mockReturnValue(cluster.promise);
	let ready = false;
	void appData.whenProjectCatalogLoaded().then(() => { ready = true; });
	const loads = Promise.all([appData.loadProjects(), appData.loadClusterProjects()]);
	local.resolve([{ id: 'p' }]); await Promise.resolve(); await Promise.resolve();
	expect(appData.projectCatalogLoaded).toBe(false); expect(ready).toBe(false);
	cluster.resolve({ projects: [{ project_v2_id: 'repo', local_project_id: 'p' }] }); await loads; await Promise.resolve();
	expect(appData.projectCatalogLoaded).toBe(true); expect(ready).toBe(true); expect(appData.projectCatalogFailed).toBe(false);
	await expect(appData.whenProjectCatalogLoaded()).resolves.toBeUndefined();
});

it('counts a failed half as answered, and says it failed, so a held send never waits forever', async () => {
	vi.spyOn(console, 'error').mockImplementation(() => {});
	mocks.get.mockResolvedValue([]); mocks.listClusterProjects.mockRejectedValue(new Error('down'));
	await Promise.all([appData.loadProjects(), appData.loadClusterProjects()]);
	expect(appData.projectCatalogLoaded).toBe(true); expect(appData.projectCatalogFailed).toBe(true);
	mocks.listClusterProjects.mockResolvedValue({ projects: [] });
	await appData.loadClusterProjects();
	expect(appData.projectCatalogFailed).toBe(false);
});
