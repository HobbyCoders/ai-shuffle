/**
 * Line-anchored review feedback for the diff viewer.
 *
 * The owner selects a range of diff lines and writes a comment; these helpers
 * turn that into one plain-text chat message for the agent (file, old/new line
 * range, review scope, a bounded diff excerpt and the comment). Same pattern as
 * the browser panel's `composeElementPrompt`: nothing is posted anywhere else
 * and nothing on disk changes. Kept pure so it is testable without a DOM.
 */
import type { DiffLine } from '$lib/utils/diff-parser';

export type ReviewScope = 'working' | 'base';

/** What the reviewed diff compares, for the message's scope line. */
export interface ReviewScopeContext {
	scope: ReviewScope;
	/** Working-scope file category: staged, modified, untracked, conflict. */
	category?: string;
	/** Base-scope ref the merge base was taken against, e.g. `origin/main`. */
	baseRef?: string | null;
	/** Base-scope merge-base commit (full SHA). */
	mergeBase?: string | null;
}

export interface LineSpan {
	start: number;
	end: number;
}

export interface SelectedRanges {
	old: LineSpan | null;
	new: LineSpan | null;
}

/** Bounds on the excerpt so a huge selection cannot flood the prompt. */
export const EXCERPT_LIMITS = { maxLines: 40, maxChars: 4000, maxLineChars: 240 } as const;

/** Lines the owner can anchor feedback to (hunk headers and gaps are not). */
export function isSelectableLine(line: { type: string }): line is DiffLine {
	return line.type === 'addition' || line.type === 'deletion' || line.type === 'context';
}

/** Order two indices into an inclusive `[start, end]` range. */
export function orderedRange(anchor: number, focus: number): LineSpan {
	return anchor <= focus ? { start: anchor, end: focus } : { start: focus, end: anchor };
}

/** Old and new file line ranges covered by the selected lines. */
export function selectedRanges(lines: readonly DiffLine[]): SelectedRanges {
	const span = (values: (number | null)[]): LineSpan | null => {
		const nums = values.filter((n): n is number => typeof n === 'number');
		return nums.length ? { start: Math.min(...nums), end: Math.max(...nums) } : null;
	};
	return {
		old: span(lines.map(l => l.oldLineNumber)),
		new: span(lines.map(l => l.newLineNumber)),
	};
}

export function formatSpan(span: LineSpan, sep = '-'): string {
	return span.start === span.end ? String(span.start) : `${span.start}${sep}${span.end}`;
}

/** `new 12-18, old 12-15`, or one side when the selection only touches it. */
export function describeRanges(ranges: SelectedRanges, sep = '-'): string {
	const parts: string[] = [];
	if (ranges.new) parts.push(`new ${formatSpan(ranges.new, sep)}`);
	if (ranges.old) parts.push(`old ${formatSpan(ranges.old, sep)}`);
	return parts.join(', ') || 'no numbered lines';
}

/** Short chip label for the UI, e.g. `L12–18` (new side first). */
export function rangeChip(ranges: SelectedRanges): string {
	const side = ranges.new ?? ranges.old;
	if (!side) return '';
	return `L${formatSpan(side, '–')}${ranges.new ? '' : ' (old)'}`;
}

export function shortSha(sha: string | null | undefined): string {
	return sha ? sha.slice(0, 7) : '';
}

/** Human description of what the diff compares. */
export function describeScope(ctx: ReviewScopeContext): string {
	if (ctx.scope === 'base') {
		const base = ctx.baseRef || 'the base branch';
		const sha = shortSha(ctx.mergeBase);
		return `changes vs ${base}${sha ? ` (merge base ${sha})` : ''}`;
	}
	switch (ctx.category) {
		case 'staged': return 'uncommitted changes (staged)';
		case 'untracked': return 'uncommitted changes (new file)';
		case 'conflict': return 'uncommitted changes (merge conflict)';
		default: return 'uncommitted changes (unstaged)';
	}
}

/** Single-line prefix used by unified diffs. */
function prefixFor(line: DiffLine): string {
	if (line.type === 'addition') return '+';
	if (line.type === 'deletion') return '-';
	if (line.type === 'header') return '';
	return ' ';
}

export interface Excerpt {
	text: string;
	/** Selected lines that did not fit in the excerpt. */
	omitted: number;
}

/**
 * Format the selected lines as a unified-diff excerpt, capped by line count,
 * total characters and per-line length. Omitted lines are counted, not hidden.
 */
export function buildExcerpt(
	lines: readonly DiffLine[],
	limits: { maxLines: number; maxChars: number; maxLineChars: number } = EXCERPT_LIMITS,
): Excerpt {
	const out: string[] = [];
	let chars = 0;
	for (const line of lines) {
		if (out.length >= limits.maxLines) break;
		let content = line.content.replace(/\r$/, '');
		if (content.length > limits.maxLineChars) content = `${content.slice(0, limits.maxLineChars)}…`;
		const row = `${prefixFor(line)}${content}`;
		if (out.length > 0 && chars + row.length + 1 > limits.maxChars) break;
		out.push(row);
		chars += row.length + 1;
	}
	return { text: out.join('\n'), omitted: lines.length - out.length };
}

/** A Markdown fence longer than any backtick run inside `text`. */
export function fenceFor(text: string): string {
	const longest = Math.max(0, ...(text.match(/`+/g) ?? []).map(run => run.length));
	return '`'.repeat(Math.max(3, longest + 1));
}

export interface DiffFeedbackInput {
	file: string;
	/** Previous path when the file was renamed. */
	originalFile?: string | null;
	lines: readonly DiffLine[];
	scope: ReviewScopeContext;
	comment: string;
}

/**
 * Compose the one chat message sent for a line-anchored review comment.
 * The owner can still edit the result before it is sent.
 */
export function composeDiffFeedbackPrompt(input: DiffFeedbackInput): string {
	const ranges = selectedRanges(input.lines);
	const excerpt = buildExcerpt(input.lines);
	const fence = fenceFor(excerpt.text);
	const renamed = input.originalFile && input.originalFile !== input.file
		? ` (renamed from \`${input.originalFile}\`)`
		: '';
	const out = [
		`Review comment on \`${input.file}\`${renamed}, lines ${describeRanges(ranges)}, in the ${describeScope(input.scope)}:`,
		'',
		`${fence}diff`,
		excerpt.text,
		fence,
	];
	if (excerpt.omitted > 0) {
		out.push(`(${excerpt.omitted} more selected line${excerpt.omitted === 1 ? '' : 's'} not shown)`);
	}
	out.push('', input.comment.trim());
	return out.join('\n').trimEnd();
}
