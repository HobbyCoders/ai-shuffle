import { expect, it } from 'vitest';
import { browserTextChunks } from './text-chunks';

it.each(['漢'.repeat(12000), '😀'.repeat(20000), '\n\r\t\u0000"\\'.repeat(9000), 'a'.repeat(50000)])(
	'fits serialized UTF-8 frames including maximum workspace identity (%#)', (text) => {
		const chunks = [...browserTextChunks(text)];
		expect(chunks.join('')).toBe(text);
		for (const chunk of chunks) {
			expect(chunk.length).toBeLessThanOrEqual(16384);
			expect(chunk).not.toMatch(/^[\uDC00-\uDFFF]|[\uD800-\uDBFF]$/u);
			const frame = { type: 'input', kind: 'text', text: chunk,
				id: '𐐀'.repeat(128), runtime_id: '𐐀'.repeat(128), tab_id: '𐐀'.repeat(128) };
			expect(new TextEncoder().encode(JSON.stringify(frame)).length).toBeLessThanOrEqual(32768);
		}
	}
);
