/** A sent prompt's first placement survives later maintenance requests in the
 * same frame (#1321). Runs MessageArea's own scheduler and cancel statements;
 * importing the component would pull in the entire live session graph. */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import ts from 'typescript';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');
const instance = source.match(/<script lang="ts">([\s\S]*?)<\/script>/)?.[1];
if (!instance) throw new Error('Missing MessageArea instance script');
const script = ts.createSourceFile('MessageArea.ts', instance, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
function declaration(start: string): string {
	const matches: string[] = [];
	function visit(node: ts.Node) {
		if (ts.isStatement(node) && node.getText().startsWith(start)) matches.push(node.getText());
		ts.forEachChild(node, visit);
	}
	visit(script);
	if (matches.length !== 1) throw new Error(`Expected one MessageArea statement: ${start} (found ${matches.length})`);
	return ts.transpile(matches[0], { target: ts.ScriptTarget.ES2022 });
}

type Scheduler = {
	schedule(initial?: boolean): void;
	cancel(): void;
	anchor(id: string | null): void;
	frame(): void;
	placements: boolean[];
};

function scheduler(): Scheduler {
	return new Function(`
		const MIN_TRANSCRIPT_TAIL_RESERVE = 24;
		let tailReservePx = MIN_TRANSCRIPT_TAIL_RESERVE, latestTurnTailPx = 0;
		let currentTurnAnchorId = 'prompt-1';
		let anchorMaintenanceRaf = null;
		${declaration('let anchorPlacementOwedFor')}
		const frames = new Map();
		let nextFrame = 1;
		const requestAnimationFrame = (fn) => { frames.set(nextFrame, fn); return nextFrame++; };
		const cancelAnimationFrame = (id) => { frames.delete(id); };
		const markProgrammaticScrollSettling = () => {};
		const placements = [];
		const maintainCurrentTurnAnchor = (initial) => { placements.push(initial); };
		${declaration('function cancelCurrentTurnAnchor(')}
		${declaration('function scheduleCurrentTurnAnchorMaintenance(')}
		return {
			schedule: (initial) => scheduleCurrentTurnAnchorMaintenance(initial),
			cancel: () => cancelCurrentTurnAnchor(false),
			anchor: (id) => { currentTurnAnchorId = id; },
			frame() { const due = [...frames.values()]; frames.clear(); due.forEach((fn) => fn()); },
			placements
		};
	`)() as Scheduler;
}

describe('MessageArea sent-prompt placement', () => {
	it('keeps the first placement when a plain maintenance request lands in the same frame', () => {
		const s = scheduler();
		s.schedule(true);
		// A send after Stop turns following back on; effects that read it ask again.
		s.schedule();
		s.frame();
		expect(s.placements).toEqual([true]);
	});

	it('places once: the next frame is plain maintenance', () => {
		const s = scheduler();
		s.schedule(true);
		s.frame();
		s.schedule();
		s.frame();
		expect(s.placements).toEqual([true, false]);
	});

	it('does not hand an owed placement to a different prompt', () => {
		const s = scheduler();
		s.schedule(true);
		// A queued prompt lands as the next anchor, which follows without placement.
		s.anchor('prompt-2');
		s.schedule();
		s.frame();
		expect(s.placements).toEqual([false]);
	});

	it('forgets the owed placement when the anchor is cancelled', () => {
		const s = scheduler();
		s.schedule(true);
		s.cancel();
		s.anchor('prompt-1');
		s.schedule();
		s.frame();
		expect(s.placements).toEqual([false]);
	});
});
