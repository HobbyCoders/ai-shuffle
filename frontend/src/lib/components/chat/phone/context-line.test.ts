import { describe, expect, it } from 'vitest';
import { composerContextLine } from './context-line';

const facts = { projectName: null, branch: null, agentName: 'Dev agent', workerName: 'Local' };

describe('composerContextLine', () => {
	it('leaves the worker to the header on a new chat', () => {
		expect(composerContextLine({ ...facts, newChat: true })).toEqual(['Dev agent', 'No project']);
		expect(composerContextLine({ ...facts, newChat: true, projectName: 'Dev sandbox', branch: 'main' }))
			.toEqual(['Dev agent', 'Dev sandbox']);
	});

	it('still names a worker on a new chat when it is not the one in the header', () => {
		expect(composerContextLine({ ...facts, newChat: true, pickedWorkerName: 'Worker B' }))
			.toEqual(['Dev agent', 'No project', 'Worker B']);
	});

	it('keeps the worker in a chat with no project, where nothing else names it', () => {
		expect(composerContextLine(facts)).toEqual(['Dev agent', 'Local']);
	});

	it('says project and branch in a chat with a project', () => {
		expect(composerContextLine({ ...facts, projectName: 'Dev sandbox', branch: 'main' }))
			.toEqual(['Dev sandbox', 'main']);
	});

	it('asks for an agent when none is known', () => {
		expect(composerContextLine({ ...facts, agentName: null, newChat: true })).toEqual(['Choose an agent', 'No project']);
	});
});
