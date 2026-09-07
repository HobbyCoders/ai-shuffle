/** Browser panel receiver limits the complete JSON UTF-8 frame to 32 KiB.
 * Reserve 2 KiB for the workspace command ID, runtime ID and tab ID (each
 * server-bounded to 128 Unicode code points), plus their JSON field overhead.
 * The provider login stream has no identity envelope and uses the same chunks.
 */
const INPUT_FRAME_BUDGET = 32768 - 2048;
const encoder = new TextEncoder();

export function* browserTextChunks(text: string): Generator<string> {
	const fits = (chunk: string) => encoder.encode(JSON.stringify({ type: 'input', kind: 'text', text: chunk })).length <= INPUT_FRAME_BUDGET;
	for (let start = 0; start < text.length;) {
		let low = 1, high = Math.min(16384, text.length - start);
		while (low < high) {
			const middle = Math.ceil((low + high) / 2);
			if (fits(text.slice(start, start + middle))) low = middle;
			else high = middle - 1;
		}
		let end = start + low;
		const last = text.charCodeAt(end - 1), next = text.charCodeAt(end);
		if (last >= 0xd800 && last <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) end--;
		yield text.slice(start, end);
		start = end;
	}
}
