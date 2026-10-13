// @vitest-environment jsdom
import { render, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import BrowserFileInput from './BrowserFileInput.svelte';
afterEach(cleanup);
const field = { id:'file-one', type:'file', label:'Documents', value:'', x:10, y:20, width:240, height:32, accept:'.txt', multiple:true, chooser:true };
it('captures the original picker callback and delivers every selected file', async () => {
  const send = vi.fn().mockResolvedValue(undefined), onActive = vi.fn();
  const onPick = vi.fn(() => send);
  const view = render(BrowserFileInput, { field, scale:1, left:0, top:0, onPick, onActive });
  const input = view.getByLabelText('Documents') as HTMLInputElement;
  expect(input.accept).toBe('.txt'); expect(input.multiple).toBe(true);
  await fireEvent.pointerDown(input);
  const files = [new File(['one'], 'one.txt'), new File(['two'], 'two.txt')];
  await fireEvent.change(input, { target: { files } });
  await waitFor(() => expect(send).toHaveBeenCalledWith(files));
  expect(onPick).toHaveBeenCalledWith(field);
  expect(onActive).toHaveBeenLastCalledWith(false);
  expect(input.value).toBe('');
});
it('cancel releases interaction without uploading and hides a custom chooser', async () => {
  const send = vi.fn(), onActive = vi.fn();
  const view = render(BrowserFileInput, { field, scale:1, left:0, top:0, onPick:() => send, onActive });
  const input = view.getByLabelText('Documents') as HTMLInputElement;
  await fireEvent.pointerDown(input);
  await fireEvent(input, new Event('cancel'));
  expect(send).not.toHaveBeenCalled(); expect(onActive).toHaveBeenLastCalledWith(false);
  expect(input.hidden).toBe(true);
});
