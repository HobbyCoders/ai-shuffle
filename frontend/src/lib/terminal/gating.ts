/**
 * Who sees terminal controls (#1945). Pure, so the rules are tested apart
 * from the store that fetches the probe.
 *
 * The probe is `GET /terminals/targets`. Its answer is the only gate the UI
 * trusts: `isAdmin` is true for passwordless local access, which the
 * terminal routes refuse, so a client-side role check would show dead
 * buttons.
 */
import type { CommandBlock } from './commands';
import type { TerminalTarget } from '$lib/api/terminals';

export type ProbeState =
	| { status: 'idle' | 'loading' }
	/** 401/403: not the owner. Show nothing but Copy. */
	| { status: 'denied' }
	/** The probe answered with an error code (for example chat_on_other_worker). */
	| { status: 'error'; code: string; message: string }
	| { status: 'ready'; targets?: TerminalTarget[]; supported: boolean; reason: string | null; worker: string; shells: string[];
		defaultShell: string | null; folder: string | null; folderSource: string | null; open: number; limit: number };

export type Access =
	/** Show no terminal controls at all. */
	| { kind: 'hidden' }
	/** Show the controls, disabled, with this reason. */
	| { kind: 'disabled'; reason: string }
	| { kind: 'ready' };

const ON_OTHER_WORKER = 'That chat runs on another worker. Terminals open on this worker only for now.';

/** Whether the panel toggle and command buttons show, and whether they work. */
export function terminalAccess(probe: ProbeState): Access {
	switch (probe.status) {
		case 'idle':
		case 'loading':
		case 'denied':
			return { kind: 'hidden' };
		case 'error':
			// A chat on another worker passed authorization (it is checked
			// first), so the owner sees why the button does nothing.
			if (probe.code === 'chat_on_other_worker') return { kind: 'disabled', reason: probe.message || ON_OTHER_WORKER };
			return { kind: 'hidden' };
		case 'ready':
			if (!probe.supported) return { kind: 'disabled', reason: probe.reason || 'Terminals are not supported on this worker yet.' };
			return { kind: 'ready' };
	}
}

/**
 * Whether one command block can open here. Phase 2 opens shells on this
 * worker only; a block that names another worker, the host or a shell this
 * worker lacks stays visible but disabled, with the reason as its title.
 */
export function blockAccess(block: CommandBlock, probe: ProbeState): Access {
	const base = terminalAccess(probe);
	if (base.kind !== 'ready' || probe.status !== 'ready') return base;
	if (block.host) return { kind: 'disabled', reason: 'Host shells are not supported yet. Terminals open on this worker only for now.' };
	if (block.on && block.on.toLowerCase() !== probe.worker.toLowerCase()) {
		return { kind: 'disabled', reason: `This command is for ${block.on}. Terminals open on ${probe.worker} only for now.` };
	}
	if (block.shell && !probe.shells.includes(block.shell)) {
		return { kind: 'disabled', reason: `${block.shell} isn't available on ${probe.worker}.` };
	}
	const problem = block.cwd ? cwdProblem(block.cwd) : null;
	if (problem) return { kind: 'disabled', reason: problem };
	return { kind: 'ready' };
}

/**
 * Characters that never show, or that change how the text around them is
 * shown: controls (Cc), format characters such as zero-width spaces, the
 * byte-order mark, soft hyphens and direction marks (Cf), line and paragraph
 * separators (Zl, Zp), and lone surrogates (Cs).
 */
const HIDDEN = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Cs}]/u;

/**
 * Why a folder from a fence (`cwd=`) can't be sent, or null when it can.
 * The strip must show exactly the folder that opens, so anything that
 * displays differently from what the server resolves is refused: hidden or
 * reordering characters, and `.` or `..` parts.
 */
export function cwdProblem(cwd: string): string | null {
	if (HIDDEN.test(cwd)) return "The folder has hidden or control characters, so it can't open from here.";
	const windows = /^[a-z]:[\\/]/i.test(cwd) || /^\\\\[^\\/]+[\\/][^\\/]+/.test(cwd);
	if (!cwd.startsWith('/') && !windows) return 'The folder must be a full path, for example /workspace/my-project or C:\\Projects.';
	if (cwd.split(windows ? /[\\/]/ : /\//).some((part) => part === '.' || part === '..')) return 'The folder has . or .. in it. Use a plain full path.';
	return null;
}

/** One run of a folder path as the strip shows it. */
export type FolderPiece =
	| { kind: 'text'; text: string }
	/** A character outside printable ASCII, or a space that is easy to miss, shown as its code point. */
	| { kind: 'char'; char: string; code: string };

const isPlain = (ch: string | undefined) => !!ch && ch >= '!' && ch <= '~';

/**
 * Split a folder into plain ASCII runs and characters that need a second
 * look. Printable ASCII (`!` to `~`) shows as itself; so does a space with a
 * plain character on each side that is not `/`. Anything else (a letter that
 * may look like an ASCII one, a no-break space, a leading, trailing or
 * doubled space) is shown as its code point, so nothing in the path is
 * invisible or a look-alike.
 */
export function folderPieces(path: string): FolderPiece[] {
	const pieces: FolderPiece[] = [];
	const chars = Array.from(path);
	let run = '';
	chars.forEach((ch, i) => {
		const prev = chars[i - 1];
		const next = chars[i + 1];
		const fine = isPlain(ch) || (ch === ' ' && isPlain(prev) && isPlain(next) && prev !== '/' && next !== '/');
		if (fine) {
			run += ch;
			return;
		}
		if (run) pieces.push({ kind: 'text', text: run });
		run = '';
		pieces.push({ kind: 'char', char: ch, code: `U+${ch.codePointAt(0)!.toString(16).toUpperCase().padStart(4, '0')}` });
	});
	if (run) pieces.push({ kind: 'text', text: run });
	return pieces;
}

/** True when the folder has any character `folderPieces` shows as a code point. */
export function hasUnusualCharacters(path: string): boolean {
	return folderPieces(path).some((piece) => piece.kind === 'char');
}

/** The folder as one line of text, with unusual characters written as ‹U+0430›. */
export function printableFolder(path: string): string {
	return folderPieces(path).map((piece) => (piece.kind === 'text' ? piece.text : `‹${piece.code}›`)).join('');
}

/** True when `path` is `root` or a folder under it. */
export function isInsideFolder(path: string, root: string | null | undefined): boolean {
	if (!root) return false;
	const base = root.replace(/\/+$/, '');
	const target = path.replace(/\/+$/, '') || '/';
	if (!base) return true; // root is "/"
	return target === base || target.startsWith(`${base}/`);
}

/** Last folder name, for tab labels and the target strip. "/" stays "/". */
export function folderName(path: string | null | undefined): string {
	if (!path) return '';
	const trimmed = path.replace(/\/+$/, '');
	if (!trimmed) return '/';
	return trimmed.slice(trimmed.lastIndexOf('/') + 1) || trimmed;
}

/** " with code 0." after "The shell exited", or "." when no code is known. */
export function exitDetail(code: number | null | undefined): string {
	return typeof code === 'number' ? ` with code ${code}.` : '.';
}

export interface BlockTarget {
	worker: string;
	shell: string;
	/** What the strip shows for the folder: the chat folder's name, or the fence's full path. */
	folder: string;
	/** The folder exactly as sent (the fence's `cwd`, or the chat's folder). */
	path: string;
	/** The folder came from the fence, not the chat. */
	fromFence: boolean;
	/** A fence folder that is not the chat's folder or under it. */
	outside: boolean;
	/** The fence folder has characters outside printable ASCII, or a space that is easy to miss. */
	unusual: boolean;
}

/** Where a block's terminal opens. Null until the probe is ready. */
export function blockTarget(block: CommandBlock, probe: ProbeState): BlockTarget | null {
	if (probe.status !== 'ready') return null;
	const fromFence = !!block.cwd;
	const path = block.cwd ?? probe.folder ?? '';
	return {
		worker: block.on ?? probe.worker,
		shell: block.shell ?? probe.defaultShell ?? '',
		folder: fromFence ? path : folderName(path),
		path,
		fromFence,
		outside: fromFence && !isInsideFolder(path, probe.folder),
		unusual: fromFence && hasUnusualCharacters(path),
	};
}

/**
 * "Unraid · task-1234 · bash", the line under a runnable block. A folder
 * from the fence shows in full, since it can be anywhere on the worker, with
 * any unusual character written as its code point.
 */
export function targetLine(block: CommandBlock, probe: ProbeState): string {
	const target = blockTarget(block, probe);
	if (!target) return '';
	return [target.worker, target.fromFence ? printableFolder(target.path) : target.folder, target.shell].filter(Boolean).join(' · ');
}
