import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import AssistantMessage from './AssistantMessage.svelte';

class FakeAudio {
	static last: FakeAudio | null = null;
	onended: (() => void) | null = null;
	onerror: (() => void) | null = null;
	currentTime = 5;
	paused = false;
	constructor(public src: string) { FakeAudio.last = this; }
	play = vi.fn(async () => {});
	pause = vi.fn(() => { this.paused = true; });
}

const finalMeta = { total_cost_usd: 0.01234, duration_ms: 2500, tokens_out: 50 };
let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
	FakeAudio.last = null;
	fetchMock = vi.fn(async () => new Response(new Blob(['mp3']), { status: 200 }));
	vi.stubGlobal('fetch', fetchMock);
	vi.stubGlobal('Audio', FakeAudio);
	URL.createObjectURL = vi.fn(() => 'blob:tts');
	URL.revokeObjectURL = vi.fn();
	vi.spyOn(console, 'error').mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('AssistantMessage footer and actions', () => {
	it('shows the model name, cost, duration and throughput once the turn is final', () => {
		render(AssistantMessage, { content: 'Done.', streaming: false, metadata: finalMeta, modelLabel: 'Opus', profileName: 'Helper' });
		expect(screen.getByText('Opus')).toBeTruthy();
		expect(screen.queryByText('Helper')).toBeNull();
		expect(screen.getByText('$0.0123')).toBeTruthy();
		expect(screen.getByText('2.5s')).toBeTruthy();
		expect(screen.getByText('20.0 t/s')).toBeTruthy();
		expect(screen.getByTitle('Read aloud')).toBeTruthy();
	});

	it('offers no actions or footer while streaming, and a placeholder only when partial messages are off', async () => {
		const view = render(AssistantMessage, { content: '', streaming: true, hasPartialMessages: false, metadata: finalMeta });
		expect(screen.getByLabelText('Streaming')).toBeTruthy();
		expect(view.container.querySelectorAll('.animate-pulse')).toHaveLength(3);
		expect(screen.queryByTitle('Copy')).toBeNull();
		expect(screen.queryByText('2.5s')).toBeNull();
		await view.rerender({ hasPartialMessages: true });
		expect(view.container.querySelectorAll('.animate-pulse')).toHaveLength(0);
	});

	it('offers copy but not read-aloud on an intermediate message without completion metadata', () => {
		render(AssistantMessage, { content: 'Thinking out loud', streaming: false, metadata: { model: 'x' } });
		expect(screen.queryByTitle('Read aloud')).toBeNull();
		expect(screen.getByRole('button', { name: 'Copy message' })).toBeTruthy();
	});

	it('reads the reply aloud as plain speech text and stops on the second press', async () => {
		render(AssistantMessage, {
			content: '## Result\n\n**Bold** move ✓ see [docs](https://x.test)\n\n```js\nconsole.log(1)\n```',
			streaming: false, metadata: finalMeta
		});
		await fireEvent.click(screen.getByTitle('Read aloud'));
		await waitFor(() => expect(FakeAudio.last?.play).toHaveBeenCalled());
		const [url, init] = fetchMock.mock.calls[0];
		expect(url).toMatch(/\/api\/v1\/settings\/tts\/stream$/);
		expect(JSON.parse(init.body)).toEqual({ text: 'Result Bold move see docs code block', output_format: 'mp3' });
		const stop = await screen.findByTitle('Stop reading');
		await fireEvent.click(stop);
		expect(FakeAudio.last!.pause).toHaveBeenCalled();
		expect(FakeAudio.last!.currentTime).toBe(0);
		expect(await screen.findByTitle('Read aloud')).toBeTruthy();
	});

	it('returns to idle when speech generation fails', async () => {
		fetchMock.mockResolvedValueOnce(new Response('nope', { status: 503 }));
		render(AssistantMessage, { content: 'Hello', streaming: false, metadata: finalMeta });
		await fireEvent.click(screen.getByTitle('Read aloud'));
		await waitFor(() => expect(console.error).toHaveBeenCalledWith('Failed to generate TTS:', expect.any(Error)));
		expect(screen.getByTitle('Read aloud')).toBeTruthy();
		expect(FakeAudio.last).toBeNull();
	});
});

describe('AssistantMessage copy', () => {
	const markdown = [
		'## Plan', '', '- **Fix** the `build`', '- see [docs](https://x.test)', '',
		'```ts', 'const a = 1;', '```'
	].join('\n');

	it('copies the Markdown source of a settled block, with or without turn metadata', async () => {
		const writeText = vi.fn(async () => {});
		vi.stubGlobal('navigator', { clipboard: { writeText } });
		for (const metadata of [undefined, finalMeta]) {
			const view = render(AssistantMessage, { content: markdown, streaming: false, metadata });
			const button = screen.getByRole('button', { name: 'Copy message' });
			await fireEvent.click(button);
			expect(writeText).toHaveBeenLastCalledWith(markdown);
			expect(await screen.findByRole('button', { name: 'Copied' })).toBeTruthy();
			view.unmount();
		}
	});

	it('hides copy while the block is streaming and shows it once it settles', async () => {
		const view = render(AssistantMessage, { content: 'Half a sen', streaming: true });
		expect(screen.queryByRole('button', { name: 'Copy message' })).toBeNull();
		await view.rerender({ content: 'Half a sentence, now whole.', streaming: false });
		expect(screen.getByRole('button', { name: 'Copy message' })).toBeTruthy();
	});

	it('renders no footer for an empty block', () => {
		const view = render(AssistantMessage, { content: '   ', streaming: false });
		expect(screen.queryByRole('button', { name: 'Copy message' })).toBeNull();
		expect(view.container.querySelector('.ameta')).toBeNull();
	});

	it('reserves the action row and reveals it on hover without remounting it', async () => {
		const view = render(AssistantMessage, { content: 'Reply', streaming: false });
		const row = view.container.querySelector('.assistant-actions')!;
		expect(row.classList.contains('is-visible')).toBe(false);
		await fireEvent.mouseEnter(view.container.querySelector('.message-container')!);
		expect(view.container.querySelector('.assistant-actions')).toBe(row);
		expect(row.classList.contains('is-visible')).toBe(true);
		await fireEvent.mouseLeave(view.container.querySelector('.message-container')!);
		expect(row.classList.contains('is-visible')).toBe(false);
	});
});
