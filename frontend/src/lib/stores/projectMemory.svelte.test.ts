/**
 * Project memory (AGENTS.md) store — load/save/delete transitions, error
 * surfacing, and the stale-response guard when switching project.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { MemoryFile, MemoryLocation } from '$lib/api/projectMemory';

const api = vi.hoisted(() => ({
	getMemoryFiles: vi.fn(),
	saveMemoryFile: vi.fn(),
	deleteMemoryFile: vi.fn()
}));

vi.mock('$lib/api/projectMemory', () => api);

import { projectMemory, allMemoryFiles, memoryError, memoryLoading, memorySaving } from './projectMemory.svelte';

function file(location: MemoryLocation, content = '', exists = content !== ''): MemoryFile {
	return {
		location, exists, content, size: content.length,
		file_path: location === 'project' ? 'AGENTS.md' : '.agents/AGENTS.md',
		updated_at: exists ? '2026-09-25T10:00:00Z' : null
	};
}

function deferred<T>() {
	let resolve!: (v: T) => void;
	let reject!: (e: unknown) => void;
	const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
	return { promise, resolve, reject };
}

beforeEach(() => {
	for (const fn of Object.values(api)) fn.mockReset();
	projectMemory.reset();
});

describe('projectMemory store', () => {
	it('starts empty', () => {
		expect(projectMemory.current).toMatchObject({ files: [], currentProjectId: null, loading: false, saving: false, error: null });
	});

	it('loads both memory locations and toggles loading', async () => {
		const pending = deferred<MemoryFile[]>();
		api.getMemoryFiles.mockReturnValue(pending.promise);
		const load = projectMemory.loadFiles('P1');
		expect(memoryLoading.current).toBe(true);
		pending.resolve([file('project', '# rules'), file('local')]);
		await load;
		expect(memoryLoading.current).toBe(false);
		expect(projectMemory.current.currentProjectId).toBe('P1');
		expect(allMemoryFiles.current.map((f) => [f.location, f.exists])).toEqual([['project', true], ['local', false]]);
	});

	it("does not let an older project's files overwrite the current project's", async () => {
		const slow = deferred<MemoryFile[]>();
		api.getMemoryFiles.mockReturnValueOnce(slow.promise).mockResolvedValueOnce([file('project', 'new')]);
		const first = projectMemory.loadFiles('OLD');
		await projectMemory.loadFiles('NEW');
		slow.resolve([file('project', 'old')]);
		await first;
		expect(projectMemory.current.currentProjectId).toBe('NEW');
		expect(projectMemory.current.files[0].content).toBe('new');
	});

	it('a stale load failure rejects its caller but leaves the newer state alone', async () => {
		const slow = deferred<MemoryFile[]>();
		api.getMemoryFiles.mockReturnValueOnce(slow.promise).mockResolvedValueOnce([file('project', 'new')]);
		const first = projectMemory.loadFiles('OLD');
		await projectMemory.loadFiles('NEW');
		const failure = { status: 500, detail: 'boom' };
		slow.reject(failure);
		await expect(first).rejects.toBe(failure);
		expect(memoryError.current).toBeNull();
		expect(projectMemory.current.files[0].content).toBe('new');
	});

	it("surfaces the backend's reason when a load fails", async () => {
		const failure = { status: 403, detail: 'Admin only' };
		api.getMemoryFiles.mockRejectedValue(failure);
		await expect(projectMemory.loadFiles('P1')).rejects.toBe(failure);
		expect(memoryError.current).toBe('Admin only');
		expect(memoryLoading.current).toBe(false);
	});

	it('save replaces only the saved location', async () => {
		api.getMemoryFiles.mockResolvedValue([file('project', 'a'), file('local', 'b')]);
		await projectMemory.loadFiles('P1');
		api.saveMemoryFile.mockResolvedValue(file('local', 'b2'));
		const saved = await projectMemory.saveFile('P1', 'local', 'b2');
		expect(api.saveMemoryFile).toHaveBeenCalledWith('P1', 'local', 'b2');
		expect(saved.content).toBe('b2');
		expect(projectMemory.current.files.map((f) => f.content)).toEqual(['a', 'b2']);
		expect(memorySaving.current).toBe(false);
	});

	it('a failed save keeps the files and shows the detail', async () => {
		api.getMemoryFiles.mockResolvedValue([file('project', 'a')]);
		await projectMemory.loadFiles('P1');
		const refusal = { status: 413, detail: 'Memory file too large' };
		api.saveMemoryFile.mockRejectedValue(refusal);
		await expect(projectMemory.saveFile('P1', 'project', 'x'.repeat(10))).rejects.toBe(refusal);
		expect(projectMemory.current.files[0].content).toBe('a');
		expect(projectMemory.current).toMatchObject({ saving: false, error: 'Memory file too large' });
		projectMemory.clearError();
		expect(memoryError.current).toBeNull();
	});

	it('delete marks the location as missing instead of dropping it', async () => {
		api.getMemoryFiles.mockResolvedValue([file('project', 'a'), file('local', 'b')]);
		await projectMemory.loadFiles('P1');
		api.deleteMemoryFile.mockResolvedValue(undefined);
		await projectMemory.deleteFile('P1', 'project');
		expect(projectMemory.current.files).toHaveLength(2);
		expect(projectMemory.current.files[0]).toMatchObject({ location: 'project', exists: false, content: '', size: 0, updated_at: null });
		expect(projectMemory.current.files[1]).toMatchObject({ location: 'local', exists: true, content: 'b' });
	});
});
