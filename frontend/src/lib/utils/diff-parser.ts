/**
 * Unified diff parser.
 *
 * Parses raw unified diff text (from `git diff`) into structured hunks
 * with line numbers for rendering in a diff viewer.
 */

export interface DiffLine {
	/** Line type: addition (+), deletion (-), context (unchanged), or hunk header */
	type: 'addition' | 'deletion' | 'context' | 'header';
	/** Raw line content (without the leading +/-/space prefix) */
	content: string;
	/** Line number in the old (before) version, null for additions and headers */
	oldLineNumber: number | null;
	/** Line number in the new (after) version, null for deletions and headers */
	newLineNumber: number | null;
}

export interface DiffHunk {
	/** Raw header line, e.g. "@@ -1,5 +1,7 @@ function foo()" */
	header: string;
	/** Start line in old file */
	oldStart: number;
	/** Line count in old file */
	oldCount: number;
	/** Start line in new file */
	newStart: number;
	/** Line count in new file */
	newCount: number;
	/** All lines in this hunk (including the header as first entry) */
	lines: DiffLine[];
}

export interface ParsedDiff {
	/** Original file path (from --- header) */
	oldFile: string | null;
	/** New file path (from +++ header) */
	newFile: string | null;
	/** Parsed hunks */
	hunks: DiffHunk[];
}

const HUNK_HEADER_RE = /^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(.*)$/;

/**
 * Parse a unified diff string into structured data.
 *
 * Handles standard `git diff` output including:
 * - File headers (diff --git, ---, +++)
 * - Multiple hunks per file
 * - New/deleted files (single-sided diffs)
 * - Binary file markers
 * - No-newline-at-end-of-file markers
 */
export function parseDiff(diffText: string): ParsedDiff {
	const lines = diffText.split('\n');
	const result: ParsedDiff = {
		oldFile: null,
		newFile: null,
		hunks: []
	};

	let currentHunk: DiffHunk | null = null;
	let oldLine = 0;
	let newLine = 0;

	for (const rawLine of lines) {
		// File headers
		if (rawLine.startsWith('--- ')) {
			const path = rawLine.slice(4);
			result.oldFile = path === '/dev/null' ? null : path.replace(/^[ab]\//, '');
			continue;
		}
		if (rawLine.startsWith('+++ ')) {
			const path = rawLine.slice(4);
			result.newFile = path === '/dev/null' ? null : path.replace(/^[ab]\//, '');
			continue;
		}

		// Skip diff --git, index, and mode lines
		if (
			rawLine.startsWith('diff --git') ||
			rawLine.startsWith('index ') ||
			rawLine.startsWith('old mode') ||
			rawLine.startsWith('new mode') ||
			rawLine.startsWith('new file mode') ||
			rawLine.startsWith('deleted file mode') ||
			rawLine.startsWith('similarity index') ||
			rawLine.startsWith('rename from') ||
			rawLine.startsWith('rename to') ||
			rawLine.startsWith('copy from') ||
			rawLine.startsWith('copy to')
		) {
			continue;
		}

		// Hunk header
		const hunkMatch = rawLine.match(HUNK_HEADER_RE);
		if (hunkMatch) {
			currentHunk = {
				header: rawLine,
				oldStart: parseInt(hunkMatch[1], 10),
				oldCount: hunkMatch[2] !== undefined ? parseInt(hunkMatch[2], 10) : 1,
				newStart: parseInt(hunkMatch[3], 10),
				newCount: hunkMatch[4] !== undefined ? parseInt(hunkMatch[4], 10) : 1,
				lines: [
					{
						type: 'header',
						content: rawLine,
						oldLineNumber: null,
						newLineNumber: null
					}
				]
			};
			oldLine = currentHunk.oldStart;
			newLine = currentHunk.newStart;
			result.hunks.push(currentHunk);
			continue;
		}

		if (!currentHunk) continue;

		// No newline at end of file marker
		if (rawLine.startsWith('\\ No newline at end of file')) {
			continue;
		}

		// Diff content lines
		if (rawLine.startsWith('+')) {
			currentHunk.lines.push({
				type: 'addition',
				content: rawLine.slice(1),
				oldLineNumber: null,
				newLineNumber: newLine++
			});
		} else if (rawLine.startsWith('-')) {
			currentHunk.lines.push({
				type: 'deletion',
				content: rawLine.slice(1),
				oldLineNumber: oldLine++,
				newLineNumber: null
			});
		} else if (rawLine.startsWith(' ') || rawLine === '') {
			// Context line (or empty line within a hunk)
			currentHunk.lines.push({
				type: 'context',
				content: rawLine.startsWith(' ') ? rawLine.slice(1) : rawLine,
				oldLineNumber: oldLine++,
				newLineNumber: newLine++
			});
		}
	}

	return result;
}

/**
 * Get a human-readable description of the diff type.
 */
export function getDiffDescription(parsed: ParsedDiff): string {
	if (!parsed.oldFile && parsed.newFile) return 'New file';
	if (parsed.oldFile && !parsed.newFile) return 'Deleted file';
	if (parsed.oldFile && parsed.newFile && parsed.oldFile !== parsed.newFile) {
		return `Renamed: ${parsed.oldFile} → ${parsed.newFile}`;
	}
	return 'Modified';
}

/**
 * Count total additions and deletions in a parsed diff.
 */
export function countChanges(parsed: ParsedDiff): { additions: number; deletions: number } {
	let additions = 0;
	let deletions = 0;
	for (const hunk of parsed.hunks) {
		for (const line of hunk.lines) {
			if (line.type === 'addition') additions++;
			else if (line.type === 'deletion') deletions++;
		}
	}
	return { additions, deletions };
}
