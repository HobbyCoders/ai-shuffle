/** What the phone composer's context line says: two facts, never a guess. */
export interface ComposerContextFacts {
	projectName: string | null;
	branch: string | null;
	agentName: string | null;
	workerName: string | null;
}

/** "Project · branch" with a project, "Agent · worker" without; empty parts drop out. */
export function composerContextLine(facts: ComposerContextFacts): string[] {
	const parts = facts.projectName ? [facts.projectName, facts.branch] : [facts.agentName ?? 'Choose an agent', facts.workerName];
	return parts.filter((part): part is string => !!part);
}
