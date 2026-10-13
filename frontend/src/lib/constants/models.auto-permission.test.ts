import {beforeEach, describe, expect, it, vi} from 'vitest';
const mocks = vi.hoisted(() => ({get:vi.fn()}));
vi.mock('$lib/api/client', () => ({api:{get:mocks.get}}));
import {hydrateModelRegistry, supportsAutoPermission} from './models';

describe('Auto permission execution capability', () => {
 beforeEach(async () => {
  mocks.get.mockResolvedValue({default:'sonnet', legacy_map:{},
   models:[{value:'sonnet',label:'Sonnet',family:'sonnet',auto_permission_supported:true}],
   providers:[
    {id:'cli',name:'Claude subscription',provider_type:'anthropic',auto_permission_supported:true,models:[]},
    {id:'api',name:'Claude API',provider_type:'anthropic',auto_permission_supported:false,models:[]},
    {id:'old',name:'Older server',provider_type:'anthropic',models:[]},
   ]});
  await hydrateModelRegistry();
 });
 it('uses the selected binding rather than inferring capability from Claude model names', () => {
  expect(supportsAutoPermission('sonnet')).toBe(true);
  expect(supportsAutoPermission('prov:cli:claude-sonnet-4-5')).toBe(true);
  expect(supportsAutoPermission('prov:api:claude-sonnet-4-5')).toBe(false);
  expect(supportsAutoPermission('prov:old:claude-sonnet-4-5')).toBe(false);
  expect(supportsAutoPermission('prov:missing:sonnet')).toBe(false);
  expect(supportsAutoPermission('gpt-5')).toBe(false);
 });
});
