import {afterEach, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/svelte';
import type {GenerationOption} from '$lib/utils/generation-choice';
const mocks = vi.hoisted(() => ({quote:vi.fn(),providers:vi.fn(),models:vi.fn(),optionFor:vi.fn()}));
vi.mock('$lib/stores/generation-choices.svelte',() => ({generationChoices:{worker:'worker-a',quote:mocks.quote,providers:mocks.providers,models:mocks.models,optionFor:mocks.optionFor}}));
vi.mock('$lib/stores/worker-presence',async () => {const {readable}=await import('svelte/store');return {workerInstalls:readable([])};});
import AutomationGenerationPicker from './AutomationGenerationPicker.svelte';
afterEach(() => {cleanup();vi.clearAllMocks();});
const option: GenerationOption = {source:'fal',account:'test-account',worker:'worker-a',model:'fal-ai/flux/dev',kind:'image',options:{count:1,transparent:false,shape:'square'},verified:true,price:{currency:'USD',unit:'item',amount:'0.02',total:'0.02',status:'estimated'},available:true,schema:{fields:[{name:'count',type:'integer',minimum:1,maximum:4,default:1},{name:'transparent',type:'boolean',default:false},{name:'shape',type:'string',enum:['square','wide'],default:'square'}]}};
describe('automation generation picker',() => {
 it('passes an exact typed choice through the handoff without generating or changing defaults',async () => {
  const onpick=vi.fn(async () => {});
  mocks.quote.mockResolvedValue({ok:true,price:option.price});
  render(AutomationGenerationPicker,{kind:'image',options:[option],onpick});
  await fireEvent.click(screen.getByRole('button',{name:'Advanced'}));
  await fireEvent.click(screen.getByRole('switch',{name:'Transparent'}));
  await fireEvent.change(screen.getByRole('combobox',{name:'Shape'}),{target:{value:'wide'}});
  await waitFor(() => expect(mocks.quote).toHaveBeenCalled());
  await waitFor(() => expect((screen.getByRole('button',{name:'Pin to this job'}) as HTMLButtonElement).disabled).toBe(false));
  await fireEvent.click(screen.getByRole('button',{name:'Pin to this job'}));
  expect(onpick).toHaveBeenCalledWith(expect.objectContaining({account:'test-account',worker:'worker-a',model:'fal-ai/flux/dev',options:{count:1,transparent:true,shape:'wide'}}));
 });
 it('keeps unpriced and unavailable options unselectable, and reports rejected saves',async () => {
  const onpick=vi.fn(async () => {throw new Error('Worker offline');});
  render(AutomationGenerationPicker,{kind:'image',options:[option,{...option,model:'gone',available:false}],onpick});
  expect((screen.getAllByRole('option')[1] as HTMLOptionElement).disabled).toBe(true);
  await fireEvent.click(screen.getByRole('button',{name:'Pin to this job'}));
  expect((await screen.findByRole('alert')).textContent).toContain('Worker offline');
 });
 it('pins a model picked from the shared provider and model browser',async () => {
  const onpick=vi.fn(async () => {});
  const kling={...option,model:'fal-ai/kling/v3',options:{shape:'wide'}};
  mocks.quote.mockResolvedValue({ok:true,price:option.price});
  mocks.providers.mockResolvedValue({kind:'image',operation:'text-to-image',operation_label:'Text to image',providers:[{source:'fal',account:'test-account',workers:['worker-a'],available:true,reason:null,native:false,model_count:1}]});
  mocks.models.mockResolvedValue({models:[{...kling,status:'available',name:'Kling v3'}],total:1,offset:0,limit:20,has_more:false});
  mocks.optionFor.mockResolvedValue(kling);
  render(AutomationGenerationPicker,{kind:'image',options:[option],onpick});
  await fireEvent.click(screen.getByRole('button',{name:'All models'}));
  await fireEvent.click(await screen.findByRole('button',{name:/Kling v3/}));
  await waitFor(() => expect(mocks.optionFor).toHaveBeenCalledWith('image',expect.objectContaining({model:'fal-ai/kling/v3'})));
  await waitFor(() => expect((screen.getByRole('button',{name:'Pin to this job'}) as HTMLButtonElement).disabled).toBe(false));
  await fireEvent.click(screen.getByRole('button',{name:'Pin to this job'}));
  expect(onpick).toHaveBeenCalledWith(expect.objectContaining({model:'fal-ai/kling/v3',options:{shape:'wide'}}));
 });
});
