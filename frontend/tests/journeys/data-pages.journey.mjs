// Content-first data destinations must expose the real content and every moved
// control without taking a panel/detail history step on responsive and native phone.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, holdBootLoad } from './lib/app.mjs';
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
   async function checkSingleTitle(label) {
    const heading=page.getByRole('heading',{level:2,name:label,exact:true});
    expect(await heading.count()===1,label+' keeps its semantic detail heading');
    const size=await heading.boundingBox();
    expect(size.width>1 && size.height>1,
     label+' has a visible range/state title distinct from the host destination');
   }
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
   await checkSingleTitle('Last 30 days');
   if(!phone) {
    await mark('align Settings and data-page titles on the shared top line');
    const statsTitle=await page.getByRole('heading',{level:2,name:'Last 30 days',exact:true}).boundingBox();
    await open('Settings');
    await page.locator('nav[aria-label=Settings]').getByRole('button',{name:/^General/}).filter({visible:true}).first().click();
    const general=page.locator('.st-page-title h1').filter({visible:true});
    await general.waitFor();
    expect((await general.boundingBox()).y===statsTitle.y,'Settings and data-page title text share the exact top line');
    const settingsFrame=await page.locator('.st-page-title').filter({visible:true}).boundingBox();
    await open('Notes');
    await page.getByRole('button',{name:'New note',exact:true}).filter({visible:true}).first().click();
    await page.getByRole('textbox',{name:'Note title',exact:true}).waitFor();
    const noteFrame=await page.locator('.st-dh').filter({visible:true}).boundingBox();
    expect(noteFrame.x===settingsFrame.x&&noteFrame.width===settingsFrame.width,
     'short Notes and long Settings reserve the same centered reading frame '+JSON.stringify({noteFrame,settingsFrame}));
    await mark('preserve real avatar and provider-logo header columns');
    await open('Agents');
    await page.locator('.st-it').filter({visible:true}).first().click();
    const avatarHeader=page.locator('.st-dh').filter({has:page.locator('.st-identity > .agent-avatar')});
    const avatar=await avatarHeader.locator('.st-identity > *').boundingBox();
    const agentTitle=await avatarHeader.locator('h2').boundingBox();
    expect(Math.abs(agentTitle.x-avatar.x-avatar.width-14)<1,'Agent avatar keeps the original 14px title gap '+JSON.stringify({avatar,agentTitle}));
    await open('Settings');
    await page.getByRole('navigation',{name:'Settings',exact:true}).getByRole('button',{name:'Providers',exact:true}).click();
    await page.locator('.pgn').first().click();
    const providerHeader=page.locator('[data-view-shell=Settings] .st-dh').filter({has:page.locator('.st-identity > *')});
    const logo=await providerHeader.locator('.st-identity > *').boundingBox();
    const providerTitle=await providerHeader.locator('h2').boundingBox();
    expect(Math.abs(providerTitle.x-logo.x-logo.width-14)<1,'Provider logo keeps the original 14px title gap '+JSON.stringify({logo,providerTitle}));
    await open('Stats');
    await page.locator('[data-view-shell="Stats"] .metrics').waitFor();
   }
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
   await page.getByRole('heading',{level:2,name:'Open pull requests',exact:true}).waitFor();
   await checkSingleTitle('Open pull requests');
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
 }, {
  // Phone list rows reach into the page gutter and pad back, so the highlight
  // keeps the desktop inset round the avatar and trailing text while every
  // panel's title stays on the shared x.
  name: 'phone-list-rows-inset',
  async setup({page,status,variant}) {
   await gotoApp(page,status,native && variant.viewport === 'phone' ? '/mobile/' : '/');
  },
  async act({page,mark,variant}) {
   if(variant.viewport!=='phone')return;
   const workspace=page.getByRole('navigation',{name:'Workspace',exact:true});
   async function open(label) {
    if(!await workspace.isVisible())await page.getByRole('button',{name:native?/^Open menu/:/^Chats(?: \(|$)|Show sidebar/}).first().click();
    const target=workspace.getByRole('button',{name:label,exact:true});
    if(await target.count())await target.click();
    else {await workspace.getByRole('button',{name:'More',exact:true}).click();await page.getByRole('menuitem',{name:label,exact:true}).click();}
   }
   const titles={};
   for(const label of ['Agents','Projects']) {
    await mark('measure '+label+' rows');
    await open(label);
    await page.locator('.view-panel .st-it').filter({visible:true}).first().waitFor();
    const m=await page.evaluate(() => {
     const vis=e=>e.getClientRects().length>0;
     const panel=[...document.querySelectorAll('.view-panel')].find(vis);
     const body=panel.querySelector('.panel-body');
     const B=e=>{const r=e.getBoundingClientRect();return {l:r.left,r:r.right};};
     const rows=[...panel.querySelectorAll('.st-it:not(.value):not(.card)')].filter(vis).map(row=>{
      const marks=[row.querySelector('.lead > *'),row.querySelector('.trailing')].filter(e=>e&&vis(e)&&e.getBoundingClientRect().width>0);
      return {row:B(row),nm:B(row.querySelector('.nm')),marks:marks.map(B)};
     });
     return {vw:innerWidth,rows,overflow:body.scrollWidth-body.clientWidth};
    });
    expect(m.rows.length>0,label+' shows rows');
    expect(m.overflow<=0,label+' list gains no sideways scroll '+m.overflow);
    for(const r of m.rows) {
     expect(r.row.l>=8&&r.row.r<=m.vw-8,label+' row highlight stays off the screen edges '+JSON.stringify(r.row));
     for(const k of r.marks)expect(k.l-r.row.l>=4&&r.row.r-k.r>=8,
      label+' avatar/trailing text sits inside the highlight with the desktop inset '+JSON.stringify({row:r.row,mark:k}));
    }
    titles[label]=m.rows.map(r=>Math.round(r.nm.l));
   }
   const xs=new Set(Object.values(titles).flat());
   expect(xs.size===1&&xs.has(38),'Agents and Projects titles share the phone title x (38) '+JSON.stringify(titles));
  }
 }, {
  name: 'providers-first-snapshot',
  throttles: ['fast', 'slow'],
  async setup({page,status,variant}) {
   // Release explicitly after inspecting the first frame; a busy runner must
   // not use up the hold before the test reaches Providers.
   await holdBootLoad(page, '/api/v1/model-providers', 10_000, {releaseOnSignal:true});
   await gotoApp(page,status,native && variant.viewport === 'phone' ? '/mobile/' : '/');
  },
  async act({page,mark,log,variant}) {
   const workspace=page.getByRole('navigation',{name:'Workspace',exact:true});
   if(!await workspace.isVisible())await page.getByRole('button',{name:native&&variant.viewport==='phone'?/^Open menu/:/^Chats(?: \(|$)|Show sidebar/}).first().click();
   await mark('open Providers before its first registry snapshot arrives');
   await workspace.getByRole('button',{name:'Settings',exact:true}).click();
   const providers=page.getByRole('navigation',{name:'Settings',exact:true}).getByRole('button',{name:'Providers',exact:true});
   await providers.click();
   await page.waitForFunction(() => window.__journeyBootHold?.startedAt !== null);
   expect(await page.getByRole('status').filter({hasText:'Loading providers…'}).isVisible(), 'the first snapshot has an explicit loading state');
   expect(await page.locator('.pl').count() === 0, 'no provisional Connected / Not connected rows can move under the user');
   // Put the response beyond Chromium's 500ms recent-input exclusion window.
   await page.waitForTimeout(700);
   await page.evaluate(() => window.__journeyBootHold.release());
   await page.waitForFunction(() => window.__journeyBootHold?.releasedAt !== null);
   await page.locator('.pl .pgn').filter({hasText:'Fake AI (dev)'}).waitFor();
   expect(await page.locator('.pl .checking').count() === 0, 'the initial account status has settled too');
   await page.waitForTimeout(700);
   log('first provider snapshot settled without provisional groups');
   await mark('a later refresh keeps the existing provider rows');
   if(variant.viewport==='phone')await page.getByRole('button',{name:'Back to Settings',exact:true}).click();
   await page.getByRole('navigation',{name:'Settings',exact:true}).getByRole('button',{name:/^General/}).filter({visible:true}).first().click();
   if(variant.viewport==='phone')await page.getByRole('button',{name:'Back to Settings',exact:true}).click();
   await providers.click();
   expect(!await page.getByText('Loading providers…',{exact:true}).count(), 'warm opens do not replace the list with loading');
   await page.locator('.pl .pgn').filter({hasText:'Fake AI (dev)'}).waitFor();
   await page.waitForTimeout(700);
  }
 }, {
  name: 'files-actions-and-card',
  async setup({page,status,variant}) {
   const phone=variant.viewport==='phone';
   await gotoApp(page,status,native && phone ? '/mobile/' : '/');
   const workspace=page.getByRole('navigation',{name:'Workspace',exact:true});
   if(!await workspace.isVisible())await page.getByRole('button',{name:native&&phone?/^Open menu/:/^Chats(?: \(|$)|Show sidebar/}).first().click();
   // Native phone keeps Files under More unless the owner pinned it.
   const files=workspace.getByRole('button',{name:'Files',exact:true});
   if(await files.isVisible())await files.click();
   else {
    await workspace.getByRole('button',{name:'More',exact:true}).click();
    await page.getByRole('menu',{name:'More destinations',exact:true}).getByRole('menuitem',{name:'Files',exact:true}).click();
   }
   await page.getByRole('button',{name:'Workspace',exact:true}).click();
   await page.locator('.fs-row:not(.fs-row--head)').first().waitFor();
  },
  async act({page,mark,variant}) {
   const phone=variant.viewport==='phone';
   await mark('Files keeps an edged card with a folder/file count');
   expect(await page.locator('.fs-main.st-card').count()===1,'F1: file list is a card');
   expect((await page.locator('.fs-footer').innerText()).includes('folders'),'F1: footer counts folders');
   const edges=await page.locator('.fs-main').evaluate(el=>{
    const card=el.getBoundingClientRect(), rows=[...el.querySelectorAll('.fs-row')];
    const first=rows[0].getBoundingClientRect(), last=rows.at(-1).getBoundingClientRect();
    return {left:first.left-card.left,right:card.right-first.right,bottom:card.bottom-last.bottom,lastBorder:getComputedStyle(rows.at(-1)).borderBottomWidth};
   });
   expect(edges.left<=1&&edges.right<=1&&edges.bottom<=1&&edges.lastBorder==='0px','D4: file rows and dividers reach the card edge without a bottom strip '+JSON.stringify(edges));
   if(phone) {
    const actions=page.locator('.fs-phone-actions');
    const upload=await actions.getByRole('button',{name:'Upload',exact:true}).boundingBox();
    const folder=await actions.getByRole('button',{name:'New folder',exact:true}).boundingBox();
    // D8: one row of two equal buttons, 8px apart, the same height.
    expect(upload&&folder&&upload.y===folder.y&&Math.abs(upload.width-folder.width)<=1&&upload.height===folder.height&&Math.abs(folder.x-(upload.x+upload.width)-8)<=1,
     'D8: Upload and New folder share one row at equal width with an 8px gap '+JSON.stringify({upload,folder}));
    // Pass 3 F3: the screen's main actions use the shared phone action size, drawn
    // the full 44px touch height (not the compact 32px row button).
    for(const label of ['Upload','New folder']) {
     const target=await actions.getByRole('button',{name:label,exact:true}).evaluate(el=>{
      const box=el.getBoundingClientRect();
      return {drawn:box.height,width:box.width,token:getComputedStyle(el).getPropertyValue('--phone-action-height').trim()};
     });
     expect(target.drawn>=44&&target.drawn<=45&&target.width>=44&&target.token==='44px','F3: file action is drawn at the shared 44px phone action size '+JSON.stringify(target));
    }
    await page.getByLabel('More file actions',{exact:true}).click();
    expect(await page.getByRole('button',{name:'Open terminal here',exact:true}).isEnabled(),'R2: guarded terminal action remains reachable from the phone bar');
    await page.getByLabel('More file actions',{exact:true}).click();
   }
   const create=page.getByRole('button',{name:'New folder',exact:true}).filter({visible:true});
   await mark('new folder keeps its original create/cancel path');
   await create.click();
   await page.getByRole('textbox',{name:'Folder name',exact:true}).fill('cancelled-r9-folder');
   await page.locator('.fs-newfolder').getByRole('button',{name:'Cancel',exact:true}).click();
   expect(!await page.locator('.fs-newfolder').count(),'folder draft can be cancelled');
  }
 }, {
  name: 'pull-request-fixed-metadata-lines',
  async setup({page,status,variant}) {
   const row={repo:'example/workspace',number:142,title:'Bring every destination onto the shared panel',url:'https://github.com/example/workspace/pull/142',author:'preview',head_branch:'feat/unified-workspace',base_branch:'main',state:'open',review:'approved',checks:'passing',updated_at:new Date().toISOString(),projects:[{id:'dev-sandbox',name:'Dev sandbox'}]};
   // The service worker forwards /api/ itself, so only a context route sees it.
   await page.context().route(url=>url.pathname==='/api/v1/github/inbox',r=>r.fulfill({json:{items:[row,{...row,number:144,title:'Keep phone navigation one step back',head_branch:'fix/a-longer-branch-name-to-check-clean-desktop-wrapping'}],repos:[row.repo],skipped:[]}}));
   await gotoApp(page,status,native&&variant.viewport==='phone'?'/mobile/':'/');
   const workspace=page.getByRole('navigation',{name:'Workspace',exact:true});
   if(!await workspace.isVisible())await page.getByRole('button',{name:/^(Open menu|Show sidebar|Chats)( \(|$)/}).first().click();
   const target=workspace.getByRole('button',{name:'Pull requests',exact:true});
   if(await target.isVisible())await target.click();
   else {await workspace.getByRole('button',{name:'More',exact:true}).click();await page.getByRole('menuitem',{name:'Pull requests',exact:true}).click();}
   await page.locator('.pri-row').first().waitFor();
  },
  async act({page,mark,variant}) {
   const phone=variant.viewport==='phone';
   await mark(phone?'phone card: title, one meta line, then the status line':'keep repository, branch and attribution on clean metadata lines');
   const rows=await page.locator('.pri-row').evaluateAll(els=>els.map(el=>{
    const box=s=>{const r=el.querySelector(s).getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};};
    const meta=el.querySelector('.pri-meta');
    return {repo:box('.pri-repo'),branch:box('.pri-branch'),by:box('.pri-attribution'),title:box('.pri-title'),external:box('.pri-external'),meta:box('.pri-meta'),badges:box('.pri-badges'),
     metaWrap:getComputedStyle(meta).whiteSpace,metaLine:parseFloat(getComputedStyle(meta).lineHeight),metaTitle:meta.title,
     clip:getComputedStyle(meta).clipPath,metaLeft:meta.getBoundingClientRect().left,
     parts:[...el.querySelectorAll('.pri-repo,.pri-branch,.pri-attribution > span')].map(s=>({left:s.getBoundingClientRect().left,dot:getComputedStyle(s,'::before').content}))};
   }));
   for(const row of rows) {
    expect(row.external.width===20&&row.external.y===row.title.y,'D1: external icon has a fixed top-aligned trailing column');
    if(phone) {
     // Council pass 3 (D6) replaces the three phone lines with one ellipsized
     // meta line; it never wraps, so a " · " can't lead a line.
     expect(row.metaWrap==='nowrap'&&row.meta.height<=row.metaLine*1.5,'D6: phone metadata is one unwrapped line '+JSON.stringify(row));
     expect(Math.abs(row.repo.y-row.branch.y)<=2,'D6: repository and branch share the meta line');
     expect(row.title.y<row.meta.y&&row.meta.y+row.meta.height<=row.badges.y+1,'D6: title, then meta, then status');
     // Council pass 5 (G4) leads the line with the branch: "head → base · repo#n · by author".
     expect(/^.+ → .+ · .+#\d+ · by /.test(row.metaTitle),'G4: the full branch-first meta stays readable in its tooltip '+row.metaTitle);
    } else {
     // Council pass 4 (S5) separates the desktop parts with middots too. Each part
     // carries its dot in a 16px seat in front, and the meta row starts 16px early
     // and clips that strip: a part that begins a wrapped line sits flush in it, so
     // its dot is hidden and no line starts with a separator (pass 1 D1).
     expect(row.parts.every(p=>p.dot!=='none'&&p.dot!=='normal'),'S5: desktop metadata parts are separated by middots '+JSON.stringify(row.parts));
     expect(row.clip==='inset(0px 0px 0px 16px)'&&Math.abs(row.metaLeft+16-row.title.x)<=1,'S5: the clipped dot strip sits just left of the title edge '+JSON.stringify({clip:row.clip,metaLeft:row.metaLeft,title:row.title.x}));
     expect(row.parts.every(p=>p.left>=row.metaLeft+16-1||Math.abs(p.left-row.metaLeft)<=1),'D1: wrapping metadata cannot create a leading separator '+JSON.stringify(row.parts));
    }
    // G4: two lines on every width, the branch first, the repository after it.
    expect(row.metaWrap==='nowrap'&&row.meta.height<=row.metaLine*1.5,'G4: the meta is one line '+JSON.stringify(row.meta));
    expect(row.branch.x<row.repo.x&&Math.abs(row.repo.y-row.branch.y)<=2,'G4: the branch leads the meta line');
   }
   if(variant.viewport==='phone')expect(rows[0].external.x===rows[1].external.x,'D1: external-link column stays in the same place');
   expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'D1: no horizontal overflow');
  }
 }, {
  name: 'worker-rows-and-one-local-remove',
  async setup({page,status,variant}) {
   const phone=variant.viewport==='phone';
   await gotoApp(page,status,native && phone ? '/mobile/' : '/');
   const workspace=page.getByRole('navigation',{name:'Workspace',exact:true});
   if(!await workspace.isVisible())await page.getByRole('button',{name:native&&phone?/^Open menu/:/^Chats(?: \(|$)|Show sidebar/}).first().click();
   await workspace.getByRole('button',{name:'Projects',exact:true}).click();
   await page.getByRole('button',{name:'Open Dev sandbox',exact:true}).click();
   await page.getByRole('tab',{name:'Workers',exact:true}).click();
   await page.locator('article[data-worker-state="ready"]').waitFor();
  },
  async act({page,mark}) {
   await mark('ready worker status follows the standard row rhythm');
   const article=page.locator('article[data-worker-state="ready"]').first();
   const status=article.locator('.st-row').filter({has:page.getByText('Status',{exact:true})});
   const folder=article.locator('.st-row').filter({has:page.getByText('Branch',{exact:true})});
   const s=await status.boundingBox(),f=await folder.boundingBox();
   expect(Math.abs(s.height-f.height)<1.1,'S4: ready Status has normal row height');
   expect(!await article.locator('.st-rowx').count(),'S4: ready Status has no empty expansion');
   await page.getByRole('button',{name:'Remove project and chats',exact:true}).waitFor();
   expect(await page.getByRole('button',{name:'Remove project and chats',exact:true}).count()===1,'S4: one removal for this worker');
   expect(!await article.getByRole('button',{name:/^Remove project and chats from/}).count(),'S4: no duplicate self-worker removal');
   const foot=await page.locator('.st-footnote').boundingBox();
   const card=await article.locator('.st-card').boundingBox();
   expect(Math.abs(foot.y-card.y-card.height-12)<1,'S4: footnote is 12px below its card');
  }
 }]
});
