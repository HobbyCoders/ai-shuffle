import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/svelte';
import {afterEach, expect, it, vi} from 'vitest';
import VoiceModelOptions from './VoiceModelOptions.svelte';
afterEach(cleanup);
const details = {model:'gpt-live-1', voices:[], transport:'stream' as const, runtime_settings:[],
 prompts:[{label:'Voice conversation prompt',key:'instructions' as const,text:'Default conversation',default_text:'Default conversation'},
 {label:'Chat agent prompt',key:'delegation_instructions' as const,text:'Default tasks',default_text:'Default tasks'}],prompt_note:'Uses the chat composer.',preview_note:''};
function setup(options = {}, onSave = vi.fn().mockResolvedValue(true)) {
 return {...render(VoiceModelOptions, {options, details, saving:false, onSave}), onSave};
}
it('shows actual factory prompts as editable text, without model or delegation settings', () => {
 setup();
 expect((screen.getByLabelText('Voice conversation prompt') as HTMLTextAreaElement).value).toBe('Default conversation');
 expect((screen.getByLabelText('Chat agent prompt') as HTMLTextAreaElement).value).toBe('Default tasks');
 expect(screen.queryByRole('combobox')).toBeNull();
});
it('saves edits and lets the user restore and save defaults', async () => {
 const {onSave} = setup({instructions:'Custom conversation'});
 await fireEvent.input(screen.getByLabelText('Voice conversation prompt'), {target:{value:'New prompt'}});
 await fireEvent.click(screen.getByRole('button', {name:'Save prompts'}));
 await waitFor(() => expect(onSave).toHaveBeenCalledWith({instructions:'New prompt',delegation_instructions:'',opening:''}));
 await fireEvent.click(screen.getByRole('button', {name:'Restore default Voice conversation prompt'}));
 expect((screen.getByLabelText('Voice conversation prompt') as HTMLTextAreaElement).value).toBe('Default conversation');
 await fireEvent.click(screen.getByRole('button', {name:'Save prompts'}));
 await waitFor(() => expect(onSave).toHaveBeenLastCalledWith({instructions:'',delegation_instructions:'',opening:''}));
});
it('keeps dirty edits on settings refresh and failed save, then discards to saved state', async () => {
 const onSave=vi.fn().mockResolvedValue(false);
 const {rerender}=setup({instructions:'Saved'},onSave);
 await fireEvent.input(screen.getByLabelText('Voice conversation prompt'), {target:{value:'Typing'}});
 await rerender({options:{instructions:'External saved'},details,saving:false,onSave});
 expect((screen.getByLabelText('Voice conversation prompt') as HTMLTextAreaElement).value).toBe('Typing');
 await fireEvent.click(screen.getByRole('button', {name:'Save prompts'}));
 await waitFor(() => expect(onSave).toHaveBeenCalled());
 expect((screen.getByLabelText('Voice conversation prompt') as HTMLTextAreaElement).value).toBe('Typing');
 await fireEvent.click(screen.getByRole('button', {name:'Discard changes'}));
 expect((screen.getByLabelText('Voice conversation prompt') as HTMLTextAreaElement).value).toBe('External saved');
});
