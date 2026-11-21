import type { ComposerSetupRow } from '../composer-shell';

/** What the phone composer's context line says: never a guess. */
export interface ComposerContextFacts {
	/** A new chat, before its first message: the header names the worker. */
	newChat?: boolean;
	projectName: string | null;
	branch: string | null;
	agentName: string | null;
	workerName: string | null;
	/** A new chat sent to a worker other than the one the header names. */
	pickedWorkerName?: string | null;
}

/** One name on the line, and the Chat controls row a tap on it opens. */
export interface ComposerContextPart {
	row: ComposerSetupRow;
	text: string;
}

/**
 * "Agent · project" (or "Agent · No project") on a new chat, plus the worker only
 * when one other than the header's was picked. In a chat, "Project · branch" with
 * a project and "Agent · worker" without. Empty parts drop out.
 */
export function composerContextLine(facts: ComposerContextFacts): ComposerContextPart[] {
	const agent = { row: 'agent', text: facts.agentName ?? 'Choose an agent' } as const;
	const parts: { row: ComposerSetupRow; text: string | null | undefined }[] = facts.newChat
		? [agent, { row: 'project', text: facts.projectName ?? 'No project' }, { row: 'worker', text: facts.pickedWorkerName }]
		: facts.projectName
			? [{ row: 'project', text: facts.projectName }, { row: 'branch', text: facts.branch }]
			: [agent, { row: 'worker', text: facts.workerName }];
	return parts.filter((part): part is ComposerContextPart => !!part.text);
}
