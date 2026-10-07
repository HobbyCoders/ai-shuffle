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

// Chat-open perf: at startup the first sync_ready refresh used to fetch both
// halves again while the boot load of the same catalog was still in flight.
it('joins a catalog request that started after the invalidation instead of repeating it', async () => {
	const invalidatedAt = performance.now();
	const local = deferred<unknown>(), cluster = deferred<unknown>();
	mocks.get.mockReturnValue(local.promise); mocks.listClusterProjects.mockReturnValue(cluster.promise);
	const boot = Promise.all([appData.loadProjects(), appData.loadClusterProjects()]);
	const refresh = appData.loadProjectCatalog(invalidatedAt);
	local.resolve([{ id: 'p' }]); cluster.resolve({ projects: [] });
	await Promise.all([boot, refresh]);
	expect(mocks.get).toHaveBeenCalledTimes(1); expect(mocks.listClusterProjects).toHaveBeenCalledTimes(1);
	expect(appData.current.projects.map(p => p.id)).toEqual(['p']);
});

it('refetches a half whose latest request predates the invalidation or failed', async () => {
	vi.spyOn(console, 'error').mockImplementation(() => {});
	mocks.get.mockResolvedValue([{ id: 'old' }]); mocks.listClusterProjects.mockRejectedValue(new Error('down'));
	await Promise.all([appData.loadProjects(), appData.loadClusterProjects()]);
	const invalidatedAt = performance.now() + 1;
	mocks.get.mockResolvedValue([{ id: 'new' }]); mocks.listClusterProjects.mockResolvedValue({ projects: [] });
	await appData.loadProjectCatalog(invalidatedAt);
	expect(mocks.get).toHaveBeenCalledTimes(2); expect(appData.current.projects.map(p => p.id)).toEqual(['new']);
	// The cluster half failed, so even a fresh-enough failure is retried.
	mocks.listClusterProjects.mockRejectedValueOnce(new Error('down'));
	const since = performance.now();
	await appData.loadClusterProjects();
	await appData.loadProjectCatalog(since);
	expect(mocks.listClusterProjects).toHaveBeenCalledTimes(4);
	expect(appData.projectCatalogFailed).toBe(false);
});
