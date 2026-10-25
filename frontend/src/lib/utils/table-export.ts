/**
 * Copy a chat table as Markdown or CSV.
 *
 * The chat renderer (chat-markdown.ts) wraps every GFM table in
 * `<div class="md-table" data-table-id="…">` and records each settled table's
 * cells here, as the markdown the agent wrote. "Copy as Markdown" rebuilds the
 * table from that source, so links, code and emphasis survive. "Copy as CSV"
 * reads the rendered cells' text, which is what a spreadsheet should get. A
 * table with no recorded source (agent-written HTML, an evicted id) falls back
 * to the rendered text for both.
 */

export type TableAlign = 'left' | 'center' | 'right' | null;

export interface TableData {
	header: string[];
	rows: string[][];
	align?: TableAlign[];
}

// ── Serializers ─────────────────────────────────────────────────────────

/** Column count: the widest of the header and every row. */
function width(data: TableData): number {
	return Math.max(data.header.length, ...data.rows.map((row) => row.length), 0);
}

function pad(cells: string[], n: number): string[] {
	return cells.length >= n ? cells.slice(0, n) : [...cells, ...Array(n - cells.length).fill('')];
}

/**
 * One markdown cell: every pipe escaped (GFM needs it even inside code
 * spans; marked hands cells over with their escapes removed), line breaks
 * kept as `<br>`.
 */
export function markdownCell(value: string): string {
	return value
		.replace(/\r\n?/g, '\n')
		.trim()
		.replace(/\|/g, '\\|')
		.replace(/\n/g, '<br>');
}

function delimiter(align: TableAlign | undefined): string {
	switch (align) {
		case 'left':
			return ':---';
		case 'center':
			return ':---:';
		case 'right':
			return '---:';
		default:
			return '---';
	}
}

/** A GFM pipe table. Short rows are padded so every row has every column. */
export function tableToMarkdown(data: TableData): string {
	const n = width(data);
	if (!n) return '';
	const line = (cells: string[]) => `| ${pad(cells, n).map(markdownCell).join(' | ')} |`;
	const align = Array.from({ length: n }, (_, i) => delimiter(data.align?.[i]));
	return [line(data.header), `| ${align.join(' | ')} |`, ...data.rows.map(line)].join('\n');
}

/**
 * Characters that make a spreadsheet read a cell as a formula (or, for tab
 * and carriage return, shift it into one). A leading apostrophe keeps the
 * cell as text: Excel, LibreOffice and Google Sheets all honour it.
 */
const FORMULA_START = /^[=+\-@\t\r]/;

/** One CSV field: formula-guarded, then quoted when it needs to be (RFC 4180). */
export function csvCell(value: string): string {
	const safe = FORMULA_START.test(value) ? `'${value}` : value;
	return /[",\r\n]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
}

/** CSV with CRLF line ends, header first. Short rows are padded. */
export function tableToCsv(data: TableData): string {
	const n = width(data);
	if (!n) return '';
	return [data.header, ...data.rows].map((row) => pad(row, n).map(csvCell).join(',')).join('\r\n');
}

// ── Reading a rendered table ────────────────────────────────────────────

/** A cell's visible text: whitespace runs collapsed, `<br>` kept as a line break. */
function cellText(cell: Element): string {
	let out = '';
	const walk = (node: Node) => {
		if (node.nodeType === 3) out += node.textContent ?? '';
		else if (node.nodeName === 'BR') out += '\n';
		else node.childNodes.forEach(walk);
	};
	walk(cell);
	return out
		.split('\n')
		.map((line) => line.replace(/\s+/g, ' ').trim())
		.join('\n')
		.trim();
}

function alignOf(cell: Element): TableAlign {
	const value = (cell.getAttribute('align') || (cell as HTMLElement).style?.textAlign || '').toLowerCase();
	return value === 'left' || value === 'center' || value === 'right' ? value : null;
}

/**
 * The rendered table as text. The header is the `<thead>` row, or the first
 * row when there is no `<thead>` (hand-written HTML tables).
 */
export function readTable(table: HTMLTableElement): TableData {
	const rows = Array.from(table.rows).filter((row) => row.closest('table') === table);
	const headRow = rows.find((row) => row.parentElement?.tagName === 'THEAD') ?? rows[0];
	if (!headRow) return { header: [], rows: [] };
	const cells = (row: HTMLTableRowElement) => Array.from(row.cells);
	return {
		header: cells(headRow).map(cellText),
		align: cells(headRow).map(alignOf),
		rows: rows.filter((row) => row !== headRow).map((row) => cells(row).map(cellText))
	};
}

// ── Source registry ─────────────────────────────────────────────────────
// Ids are random, so HTML an agent writes cannot point a menu at a table it
// did not render. The same table rendered again reuses its id.

const MAX_TABLES = 2000;
const byId = new Map<string, TableData>();
const idByContent = new Map<string, string>();

function randomId(): string {
	const bytes = new Uint8Array(9);
	globalThis.crypto.getRandomValues(bytes);
	return 'tb' + Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

/** Record a table's markdown source and return its id. Oldest go first past the cap. */
export function registerTable(data: TableData): string {
	const key = JSON.stringify(data);
	const known = idByContent.get(key);
	if (known && byId.has(known)) return known;
	const id = randomId();
	byId.set(id, data);
	idByContent.set(key, id);
	while (byId.size > MAX_TABLES) byId.delete(byId.keys().next().value as string);
	while (idByContent.size > MAX_TABLES) idByContent.delete(idByContent.keys().next().value as string);
	return id;
}

/** The recorded source for a table id, or null (unknown, forged or evicted). */
export function lookupTable(id: string | null | undefined): TableData | null {
	return (id && byId.get(id)) || null;
}

/** Test hook. */
export function clearTables(): void {
	byId.clear();
	idByContent.clear();
}

/** What each menu item copies for one rendered table. */
export function tableClipboardText(wrapper: Element, format: 'markdown' | 'csv'): string {
	const table = wrapper.querySelector('table');
	if (!table) return '';
	if (format === 'csv') return tableToCsv(readTable(table));
	const source = lookupTable((wrapper as HTMLElement).dataset?.tableId);
	return tableToMarkdown(source ?? readTable(table));
}
