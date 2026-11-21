import { describe, expect, it } from 'vitest';
import { composerContextLine, type ComposerContextFacts } from './context-line';

const facts = { projectName: null, branch: null, agentName: 'Dev agent', workerName: 'Local' };
const texts = (f: ComposerContextFacts) => composerContextLine(f).map((part) => part.text);

describe('composerContextLine', () => {
	it('leaves the worker to the header on a new chat', () => {
		expect(texts({ ...facts, newChat: true })).toEqual(['Dev agent', 'No project']);
		expect(texts({ ...facts, newChat: true, projectName: 'Dev sandbox', branch: 'main' }))
			.toEqual(['Dev agent', 'Dev sandbox']);
	});

	it('still names a worker on a new chat when it is not the one in the header', () => {
		expect(texts({ ...facts, newChat: true, pickedWorkerName: 'Worker B' }))
			.toEqual(['Dev agent', 'No project', 'Worker B']);
	});

	it('keeps the worker in a chat with no project, where nothing else names it', () => {
		expect(texts(facts)).toEqual(['Dev agent', 'Local']);
	});

	it('says project and branch in a chat with a project', () => {
		expect(texts({ ...facts, projectName: 'Dev sandbox', branch: 'main' }))
			.toEqual(['Dev sandbox', 'main']);
	});

	it('asks for an agent when none is known', () => {
		expect(texts({ ...facts, agentName: null, newChat: true })).toEqual(['Choose an agent', 'No project']);
	});

	it('ties each name to the Chat controls row it opens', () => {
		expect(composerContextLine({ ...facts, newChat: true, pickedWorkerName: 'Worker B' }).map((p) => p.row))
			.toEqual(['agent', 'project', 'worker']);
		expect(composerContextLine({ ...facts, projectName: 'Dev sandbox', branch: 'main' }).map((p) => p.row))
			.toEqual(['project', 'branch']);
		expect(composerContextLine(facts).map((p) => p.row)).toEqual(['agent', 'worker']);
	});
});
