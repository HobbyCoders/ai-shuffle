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

/**
 * "Agent · project" (or "Agent · No project") on a new chat, plus the worker only
 * when one other than the header's was picked. In a chat, "Project · branch" with
 * a project and "Agent · worker" without. Empty parts drop out.
 */
export function composerContextLine(facts: ComposerContextFacts): string[] {
	const agent = facts.agentName ?? 'Choose an agent';
	const parts = facts.newChat
		? [agent, facts.projectName ?? 'No project', facts.pickedWorkerName]
		: facts.projectName
			? [facts.projectName, facts.branch]
			: [agent, facts.workerName];
	return parts.filter((part): part is string => !!part);
}
