/**
 * A plugin README as HTML: fenced code, headings, lists, rules, bold,
 * italic, inline code and links. Callers sanitize the result.
 * (Lifted from components/plugins/PluginDetails.svelte.)
 */
export function renderReadme(text: string): string {
	if (!text) return '';

	const codeBlocks: string[] = [];
	const processed = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_match, lang: string, code: string) => {
		const escaped = code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
		const langAttr = lang ? ` data-lang="${lang}"` : '';
		const placeholder = `\x00CODEBLOCK_${codeBlocks.length}\x00`;
		codeBlocks.push(`<pre${langAttr}><code>${escaped.trimEnd()}</code></pre>`);
		return placeholder;
	});

	const lines = processed.split('\n');
	const out: string[] = [];
	let paragraph: string[] = [];
	let i = 0;

	function inline(str: string): string {
		return str
			.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
			.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>')
			.replace(/`([^`]+)`/g, '<code>$1</code>')
			.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
	}

	function flush() {
		if (paragraph.length) {
			out.push(`<p>${inline(paragraph.join('\n'))}</p>`);
			paragraph = [];
		}
	}

	while (i < lines.length) {
		const line = lines[i];
		const code = line.match(/^\x00CODEBLOCK_(\d+)\x00$/);
		if (code) {
			flush();
			out.push(codeBlocks[parseInt(code[1])]);
			i++;
			continue;
		}
		if (line.trim() === '') {
			flush();
			i++;
			continue;
		}
		const heading = line.match(/^(#{1,3}) (.+)$/);
		if (heading) {
			flush();
			const level = heading[1].length + 2; // h3..h5 inside a detail
			out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
			i++;
			continue;
		}
		if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
			flush();
			out.push('<hr>');
			i++;
			continue;
		}
		if (/^[-*] /.test(line.trim())) {
			flush();
			const items: string[] = [];
			while (i < lines.length && /^[-*] /.test(lines[i].trim())) {
				items.push(`<li>${inline(lines[i].trim().replace(/^[-*] /, ''))}</li>`);
				i++;
			}
			out.push(`<ul>${items.join('')}</ul>`);
			continue;
		}
		if (/^\d+\. /.test(line.trim())) {
			flush();
			const items: string[] = [];
			while (i < lines.length && /^\d+\. /.test(lines[i].trim())) {
				items.push(`<li>${inline(lines[i].trim().replace(/^\d+\. /, ''))}</li>`);
				i++;
			}
			out.push(`<ol>${items.join('')}</ol>`);
			continue;
		}
		paragraph.push(line);
		i++;
	}
	flush();
	return out.join('\n');
}
