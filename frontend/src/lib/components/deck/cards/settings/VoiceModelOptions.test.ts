import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/svelte';
import {afterEach,expect,it,vi} from 'vitest';
import type {VoiceOptions} from '$lib/api/voice';
vi.mock('$lib/constants/models.svelte',()=>({
 getModels:()=>[{value:'native',label:'Native model'}],
 getProviders:()=>[{id:'a',name:'OpenAI A',provider_type:'openai',auth_kind:'api',models:[{id:'model-a',label:'Model A'},{id:'model-b',label:'Model B'}]},{id:'b',name:'OpenAI B',provider_type:'openai',auth_kind:'api',models:[{id:'other',label:'Other model'}]}],
 encodeProviderModel:(p:string,m:string)=>`prov:${p}:${m}`,
}));
vi.mock('$lib/constants/model-effort',()=>({resolveModelEffort:(model:string)=>({options:(model?.endsWith('model-a')?['low','high']:[]).map(value=>({value,label:value})),explanation:model?.endsWith('model-a')?undefined:'Reasoning capabilities unknown.'})}));
import VoiceModelOptions from './VoiceModelOptions.svelte';
afterEach(cleanup);
const defaults:VoiceOptions={instructions:'',transcription_model:'gpt-4o-mini-transcribe',max_output_tokens:'inf',turn_detection:'semantic_vad',eagerness:'medium',threshold:0.5,prefix_padding_ms:300,silence_duration_ms:500,idle_timeout_ms:null};
const details={model:'gpt-realtime-2',voices:[],transport:'webrtc' as const,runtime_settings:[],prompts:[],prompt_note:'',preview_note:'',transcription_models:['gpt-realtime-whisper','gpt-transcribe','gpt-4o-mini-transcribe']};
function setup(model='gpt-realtime-2',options:VoiceOptions=defaults){
 const onSave=vi.fn().mockResolvedValue(true);
 const props={model,providerId:'a',options,details,saving:false,onSave};
 return {...render(VoiceModelOptions,props),onSave,props};
}
it('edits normal VAD tuning, transcription, finite tokens and idle timeout in one save',async()=>{
 const view=setup();
 await fireEvent.change(screen.getByLabelText('Turn detection'),{target:{value:'server_vad'}});
 expect(screen.queryByLabelText('Eagerness')).toBeNull();
 await fireEvent.input(screen.getByRole('slider',{name:'Threshold'}),{target:{value:'0.42'}});
 await fireEvent.input(screen.getByLabelText('Prefix padding (ms)'),{target:{value:'250'}});
 await fireEvent.input(screen.getByLabelText('Silence duration (ms)'),{target:{value:'400'}});
 await fireEvent.click(screen.getByLabelText('Enable idle timeout'));
 await fireEvent.input(screen.getByLabelText('Idle timeout (ms)'),{target:{value:'7000'}});
 await fireEvent.click(screen.getByLabelText('Unlimited output tokens'));
 await fireEvent.input(screen.getByLabelText('Max output tokens'),{target:{value:'1024'}});
 await fireEvent.change(screen.getByLabelText('Transcription model'),{target:{value:'gpt-realtime-whisper'}});
 expect(view.onSave).not.toHaveBeenCalled();
 await fireEvent.click(screen.getByRole('button',{name:'Save model settings'}));
 expect(view.onSave).toHaveBeenCalledWith({turn_detection:'server_vad',threshold:0.42,prefix_padding_ms:250,silence_duration_ms:400,idle_timeout_ms:7000,max_output_tokens:1024,transcription_model:'gpt-realtime-whisper'});
});
it('keeps normal tuning when switching to semantic and can disable the timeout',async()=>{
 const view=setup('gpt-realtime-2',{...defaults,turn_detection:'server_vad',threshold:0.7,idle_timeout_ms:6000});
 await fireEvent.click(screen.getByLabelText('Enable idle timeout'));
 expect(screen.queryByLabelText('Idle timeout (ms)')).toBeNull();
 await fireEvent.change(screen.getByLabelText('Turn detection'),{target:{value:'semantic_vad'}});
 expect(screen.queryByLabelText('Threshold')).toBeNull();
 await fireEvent.change(screen.getByLabelText('Eagerness'),{target:{value:'high'}});
 await fireEvent.click(screen.getByRole('button',{name:'Save model settings'}));
 expect(view.onSave).toHaveBeenCalledWith({turn_detection:'semantic_vad',eagerness:'high',idle_timeout_ms:null});
});
it('preserves a draft across remote refresh and a failed save; cancel loads current settings',async()=>{
 const view=setup(); view.onSave.mockResolvedValue(false);
 await fireEvent.input(screen.getByLabelText('Voice agent prompt'),{target:{value:'My unsaved prompt'}});
 await view.rerender({...view.props,options:{...defaults,instructions:'Changed elsewhere'}});
 expect((screen.getByLabelText('Voice agent prompt') as HTMLTextAreaElement).value).toBe('My unsaved prompt');
 await fireEvent.click(screen.getByRole('button',{name:'Save model settings'}));
 expect(screen.getByText('Unsaved changes')).toBeTruthy();
 await fireEvent.click(screen.getByRole('button',{name:'Cancel changes'}));
 expect((screen.getByLabelText('Voice agent prompt') as HTMLTextAreaElement).value).toBe('Changed elsewhere');
});
it('shows Live opening, separate prompts, scoped Responses model and model-specific effort',async()=>{
 const view=setup('gpt-live-1',{instructions:'',opening:'',delegation_type:'client',delegation_model:'',delegation_effort:'',delegation_instructions:''});
 expect(screen.queryByLabelText('Turn detection')).toBeNull();
 expect(screen.queryByLabelText('Transcription model')).toBeNull();
 await fireEvent.input(screen.getByLabelText('Opening words'),{target:{value:'Hello caller.'}});
 await fireEvent.input(screen.getByLabelText('Voice agent prompt'),{target:{value:'Speak naturally.'}});
 await fireEvent.input(screen.getByLabelText('Delegated agent prompt'),{target:{value:'Give verified results.'}});
 await fireEvent.change(screen.getByLabelText('Delegation type'),{target:{value:'responses'}});
 expect(screen.queryByRole('option',{name:'Other model'})).toBeNull();
 expect(screen.queryByRole('option',{name:'Native model'})).toBeNull();
 await fireEvent.change(screen.getByLabelText('Delegated model'),{target:{value:'prov:a:model-a'}});
 expect(screen.getByRole('option',{name:'high'})).toBeTruthy();
 await fireEvent.change(screen.getByLabelText('Reasoning effort'),{target:{value:'high'}});
 await fireEvent.click(screen.getByRole('button',{name:'Save model settings'}));
 expect(view.onSave).toHaveBeenCalledWith({opening:'Hello caller.',instructions:'Speak naturally.',delegation_instructions:'Give verified results.',delegation_type:'responses',delegation_model:'prov:a:model-a',delegation_effort:'high'});
});
it('shows only the prompt override for PersonaPlex',()=>{
 setup('fal-ai/personaplex/realtime',{instructions:''});
 expect(screen.getByLabelText('Voice agent prompt')).toBeTruthy();
 expect(screen.queryByLabelText('Opening words')).toBeNull();
 expect(screen.queryByLabelText('Turn detection')).toBeNull();
 expect(screen.queryByLabelText('Delegation type')).toBeNull();
});
