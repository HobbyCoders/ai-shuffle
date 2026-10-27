import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// The phone composer's single-line row: + | text | circles. The circles are
// bottom-aligned in the row, so the text shares their centre only when the
// field is exactly as tall as a circle. A taller field lifted the text above
// the circles' centre and left more room above them than below.
const mobileCss = readFileSync(resolve(process.cwd(), 'src/mobile.css'), 'utf8');
const MOBILE = "html:root[data-platform='mobile']";

/** The declarations of the rule whose selector list starts with `head`. */
function rule(head: string): string {
	const start = mobileCss.indexOf(head);
	expect(start, `${head} rule`).toBeGreaterThan(-1);
	return mobileCss.slice(start, mobileCss.indexOf('}', start));
}

function px(block: string, property: string): number[] {
	const match = block.match(new RegExp(`(?:^|[\\s;{])${property}:\\s*([^;]+);`));
	expect(match, `${property} in ${block}`).not.toBeNull();
	return match![1].trim().split(/\s+/).map((part) => parseFloat(part));
}

/** Top and bottom of a `padding` shorthand. */
function paddingBlock(values: number[]): [number, number] {
	return [values[0], values[2] ?? values[0]];
}

describe('phone composer single-line row', () => {
	it('centres the text line on the 40px circles with equal room above and below', () => {
		const circle = px(rule(`${MOBILE} .send-btn,\n`), 'height')[0];
		const line = px(rule(`${MOBILE} .chat-input-wrapper .tiptap-editor .ProseMirror,\n`), 'line-height')[0];
		const area = rule(`${MOBILE} .input-area {`);
		const [fieldTop, fieldBottom] = paddingBlock(px(rule(`${MOBILE} .input-area .textarea-wrapper {`), 'padding'));
		const [rowTop, rowBottom] = paddingBlock(px(area, 'padding'));

		expect(circle).toBe(40);
		expect(area).toMatch(/align-items:\s*flex-end;/);
		// The field is the circles' height, with its line in the middle.
		expect(fieldTop).toBe(fieldBottom);
		expect(fieldTop + line + fieldBottom).toBe(circle);
		// Same room above the circles (card edge) as below (quick bar).
		expect(rowTop).toBe(rowBottom);
	});
});
