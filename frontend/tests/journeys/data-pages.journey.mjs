// Content-first data destinations must expose the real content and every moved
// control without taking a panel/detail history step on responsive and native phone.
import { runJourney } from './runner.mjs';
import { expect, gotoApp } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
const native = process.argv.includes('--mobile-shell');
await runJourney({
 name: 'data-pages',
 viewports: ['desktop', 'phone'],
 throttles: ['fast'],
 allow: [...DEV_INSTANCE_NOISE, {
  kind: 'failed_requests', status: 401, url: /\/github\/inbox(\?|$)/,
  reason: 'the disposable dev instance has no GitHub sign-in; the rendered sign-in error is expected here'
 }],
 cases: [{
  name: 'content-and-controls-first',
  async setup({page,status,variant}) {
   await gotoApp(page,status,native && variant.viewport === 'phone' ? '/mobile/' : '/');
  },
  async act({page,mark,variant}) {
   const phone=variant.viewport==='phone';
   const workspace=page.getByRole('navigation',{name:'Workspace',exact:true});
   async function open(label) {
    if(!await workspace.isVisible())await page.getByRole('button',{name:native&&phone?/^Open menu/:/^Chats(?: \(|$)|Show sidebar/}).first().click();
    const target=workspace.getByRole('button',{name:label,exact:true});
    if(await target.count())await target.click();
    else {await workspace.getByRole('button',{name:'More',exact:true}).click();await page.getByRole('menuitem',{name:label,exact:true}).click();}
   }
   await mark('open Stats directly on content');
   await open('Stats');
   await page.locator('[data-view-shell="Stats"] .metrics').waitFor();
   if(phone) {
    expect(await page.locator('[data-view-shell="Stats"]').getAttribute('data-pane')==='detail','Stats opens directly on KPIs');
    expect(!await page.locator('[data-view-shell="Stats"] .st-back-row').count(),'Stats adds no Back row');
    expect(!await page.getByRole('button',{name:'Usage by provider',exact:true}).count(),'phone has no jump-link menu');
   }
   await mark('change range and keep export available');
   await page.getByRole('button',{name:'7 days',exact:true}).filter({visible:true}).click();
   await page.locator('[data-view-shell="Stats"] .metrics').waitFor();
   expect(await page.getByRole('button',{name:'Export usage by provider as CSV',exact:true}).filter({visible:true}).isEnabled(),'export remains reachable');
   await mark('open Pull requests directly on content');
   await open('Pull requests');
   await page.getByRole('heading',{level:2,name:'Pull requests',exact:true}).waitFor();
   if(phone)expect(!await page.locator('[data-view-shell="Pull requests"] .st-back-row').count(),'Pull requests adds no Back row');
   for(const name of ['Merged','Closed','All','Open']) {
    const control=page.getByRole('button',{name,exact:true}).filter({visible:true});
    expect(await control.isVisible(),name+' remains reachable');
    if(phone)expect((await control.boundingBox()).height>=44,name+' has a 44px phone target');
    await control.click();
   }
   await mark('open filters, change one and close the sheet');
   await page.getByRole('button',{name:'Filter pull requests',exact:true}).click();
   await page.getByRole('combobox',{name:'Checks',exact:true}).selectOption('failing');
   await page.getByRole('button',{name:'Done',exact:true}).click();
   expect(await page.getByRole('button',{name:'Filter pull requests, 1 active',exact:true}).isVisible(),'active filter count survives closing the sheet');
   await mark('clear filters through the same sheet');
   await page.getByRole('button',{name:'Filter pull requests, 1 active',exact:true}).click();
   await page.getByRole('button',{name:'Clear filters',exact:true}).click();
   await page.getByRole('button',{name:'Done',exact:true}).click();
   expect(await page.getByRole('button',{name:'Filter pull requests',exact:true}).isVisible(),'filters can be cleared');
  }
 }]
});
