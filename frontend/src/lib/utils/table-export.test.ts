import { afterEach, describe, expect, it } from 'vitest';
import {
	clearTables,
	csvCell,
	lookupTable,
	markdownCell,
	readTable,
	registerTable,
	tableClipboardText,
	tableToCsv,
	tableToMarkdown
} from './table-export';
import { renderChatMarkdown } from './chat-markdown';
import { sanitizeHtml } from './sanitize';

afterEach(() => clearTables());

function html(markup: string): HTMLElement {
	const host = document.createElement('div');
	host.innerHTML = markup;
	return host;
}

describe('tableToMarkdown', () => {
	it('writes a GFM table with alignment', () => {
		expect(
			tableToMarkdown({
				header: ['Name', 'Count', 'Note'],
				align: ['left', 'right', 'center'],
				rows: [['a', '1', 'x']]
			})
		).toBe('| Name | Count | Note |\n| :--- | ---: | :---: |\n| a | 1 | x |');
	});

	it('escapes pipes, keeps line breaks and pads short rows', () => {
		expect(tableToMarkdown({ header: ['A', 'B'], rows: [['x | y'], ['line 1\nline 2', '`a|b`']] })).toBe(
			'| A | B |\n| --- | --- |\n| x \\| y |  |\n| line 1<br>line 2 | `a\\|b` |'
		);
	});

	it('returns nothing for an empty table', () => {
		expect(tableToMarkdown({ header: [], rows: [] })).toBe('');
	});

	it('trims cells', () => {
		expect(markdownCell('  padded  ')).toBe('padded');
	});
});

describe('csvCell formula guard', () => {
	it.each([
		['=SUM(A1:A2)', "'=SUM(A1:A2)"],
		['+1', "'+1"],
		['-2', "'-2"],
		['@cmd', "'@cmd"],
		['\tx', "'\tx"]
	])('prefixes %j', (input, output) => {
		expect(csvCell(input)).toBe(output);
	});

	it('guards a leading carriage return and still quotes it', () => {
		expect(csvCell('\r=1')).toBe(`"'\r=1"`);
	});

	it('quotes commas, quotes and line breaks', () => {
		expect(csvCell('a,b')).toBe('"a,b"');
		expect(csvCell('say "hi"')).toBe('"say ""hi"""');
		expect(csvCell('two\nlines')).toBe('"two\nlines"');
	});

	it('guards and quotes together', () => {
		expect(csvCell('=HYPERLINK("http://x","y")')).toBe(`"'=HYPERLINK(""http://x"",""y"")"`);
	});

	it('leaves ordinary text and inner signs alone', () => {
		expect(csvCell('plain')).toBe('plain');
		expect(csvCell('a=b')).toBe('a=b');
		expect(csvCell('')).toBe('');
	});
});

describe('tableToCsv', () => {
	it('uses CRLF, header first, and pads short rows', () => {
		expect(tableToCsv({ header: ['A', 'B'], rows: [['1'], ['=2', 'x,y']] })).toBe("A,B\r\n1,\r\n'=2,\"x,y\"");
	});
});

describe('readTable', () => {
	it('reads the rendered text, alignment and line breaks', () => {
		const table = html(
			'<table><thead><tr><th align="left">Name</th><th align="right">Total</th></tr></thead>' +
				'<tbody><tr><td><strong>Ada</strong>   L.</td><td>1<br>2</td></tr></tbody></table>'
		).querySelector('table')!;
		expect(readTable(table)).toEqual({ header: ['Name', 'Total'], align: ['left', 'right'], rows: [['Ada L.', '1\n2']] });
	});

	it('takes the first row as the header when there is no thead', () => {
		const table = html('<table><tr><td>h</td></tr><tr><td>v</td></tr></table>').querySelector('table')!;
		expect(readTable(table)).toMatchObject({ header: ['h'], rows: [['v']] });
	});

	it('ignores rows of a table nested in a cell', () => {
		const table = html(
			'<table><thead><tr><th>Outer</th></tr></thead><tbody><tr><td><table><tr><td>inner</td></tr></table></td></tr></tbody></table>'
		).querySelector('table')!;
		expect(readTable(table).rows).toEqual([['inner']]);
	});
});

describe('registry', () => {
	it('reuses the id for the same table and returns null for unknown ids', () => {
		const data = { header: ['a'], rows: [['b']] };
		const id = registerTable(data);
		expect(registerTable({ header: ['a'], rows: [['b']] })).toBe(id);
		expect(lookupTable(id)).toEqual(data);
		expect(lookupTable('tbforged')).toBeNull();
		expect(lookupTable(undefined)).toBeNull();
	});
});

describe('chat markdown tables', () => {
	const source = '| Tool | Link |\n| :--- | ---: |\n| `a\\|b` | [docs](https://example.com) |\n| =1+1 | plain |';

	it('wraps a settled table and records its markdown source', () => {
		const root = html(sanitizeHtml(renderChatMarkdown(source, true)));
		const wrapper = root.querySelector<HTMLElement>('.md-table')!;
		expect(wrapper.querySelector(':scope > table')).toBeTruthy();
		expect(wrapper.dataset.tableId).toMatch(/^tb[0-9a-f]+$/);
		expect(tableClipboardText(wrapper, 'markdown')).toBe(
			'| Tool | Link |\n| :--- | ---: |\n| `a\\|b` | [docs](https://example.com) |\n| =1+1 | plain |'
		);
		expect(tableClipboardText(wrapper, 'csv')).toBe("Tool,Link\r\na|b,docs\r\n'=1+1,plain");
	});

	it('records nothing for the streaming tail', () => {
		const root = html(renderChatMarkdown(source, false));
		const wrapper = root.querySelector<HTMLElement>('.md-table')!;
		expect(wrapper.dataset.tableId).toBeUndefined();
		// Falls back to the rendered text: the link and code marks are gone.
		expect(tableClipboardText(wrapper, 'markdown')).toBe(
			'| Tool | Link |\n| :--- | ---: |\n| a\\|b | docs |\n| =1+1 | plain |'
		);
	});

	it('falls back to rendered text for a table with a forged id', () => {
		const root = html(
			'<div class="md-table" data-table-id="tbforged"><table><thead><tr><th>A</th></tr></thead><tbody><tr><td>b</td></tr></tbody></table></div>'
		);
		expect(tableClipboardText(root.firstElementChild!, 'markdown')).toBe('| A |\n| --- |\n| b |');
	});
});
