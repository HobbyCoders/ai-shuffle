import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), del: vi.fn() }));
vi.mock('./client', () => ({ api: { get: mocks.get, put: mocks.put, delete: mocks.del } }));

import { commitJobChoiceDraft, getJobChoices, saveJobChoice } from './generation-job-choices';

const JOB = '11111111-2222-4333-8444-555555555555';
const CHOICE = { source: 'fal', account: 'fal-ai', worker: 'worker-b', model: 'fal-ai/flux/dev', options: {} };

beforeEach(() => vi.clearAllMocks());

describe('generation job choices API', () => {
	it('reads the per-kind map', async () => {
		mocks.get.mockResolvedValue({ job_id: JOB, choices: { image: CHOICE } });
		expect(await getJobChoices(JOB)).toEqual({ image: CHOICE });
		expect(mocks.get).toHaveBeenCalledWith(`/generation/jobs/${JOB}/choices`);
	});

	it('refuses a choice that does not pin its account and worker before calling the API', async () => {
		await expect(saveJobChoice(JOB, 'image', { model: 'm', options: {} })).rejects.toThrow(/account, holding worker/);
		expect(mocks.put).not.toHaveBeenCalled();
	});

	it('drops origin from the body', async () => {
		mocks.put.mockResolvedValue({ choice: CHOICE });
		await saveJobChoice(JOB, '3d', { ...CHOICE, origin: 'settings' });
		expect(mocks.put).toHaveBeenCalledWith(`/generation/jobs/${JOB}/choices/3d`, CHOICE);
	});

	it('writes each staged kind and keeps only the failed ones for a retry', async () => {
		mocks.put.mockImplementation((url: string) =>
			url.endsWith('/video') ? Promise.reject(new Error('worker offline')) : Promise.resolve({ choice: CHOICE })
		);
		mocks.del.mockResolvedValue({});
		const result = await commitJobChoiceDraft(
			JOB,
			{ video: CHOICE, image: CHOICE, audio: null },
			(e) => (e instanceof Error ? e.message : 'failed')
		);
		expect(result.remaining).toEqual({ video: CHOICE });
		expect(result.errors).toEqual(['video: worker offline']);
		expect(result.applied).toEqual({ image: CHOICE, audio: null });
		expect(mocks.put).toHaveBeenCalledWith(`/generation/jobs/${JOB}/choices/image`, CHOICE);
		expect(mocks.del).toHaveBeenCalledWith(`/generation/jobs/${JOB}/choices/audio`);
	});
});
