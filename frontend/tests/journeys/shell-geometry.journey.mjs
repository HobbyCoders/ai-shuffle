// Settings title geometry and the persistent medium-width desktop frame.
import { runJourney } from './runner.mjs';
import { expect, gotoApp } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
const mobileShell = process.argv.includes('--mobile-shell');
const widths = mobileShell ? [390] : [1440, 1000, 800, 768, 390];
const shown = locator => locator.filter({visible:true}).first();
await runJourney({name:'shell-geometry', allow:[...DEV_INSTANCE_NOISE, {
 kind:'failed_requests', status:403, url:/\/mobile\/hub\/devices\/confirmations(?:\?|$)/,
 reason:'Devices asks the hub for phone confirmations; the preview cookie cannot access this owner-only endpoint'
}], cases:
 ['dark','light'].flatMap(theme => widths.map(width => ({
  name:`${mobileShell?'native':'responsive'}-${theme}-${width}`, viewports:[width < 600 ? 'phone' : 'desktop'],
  async setup({page,status}) {
   await page.setViewportSize({width,height:900});
   await page.addInitScript(value=>localStorage.setItem('ai-shuffle-theme',value),theme);
   await gotoApp(page,status);
   if(mobileShell) await gotoApp(page,status,'/mobile/');
   const nav=page.getByRole('navigation',{name:'Workspace',exact:true});
   if(!await nav.isVisible()) await page.getByRole('button',{name:/^(Chats|Open menu)( \(|$)/}).first().click();
   await nav.getByRole('button',{name:'Settings',exact:true}).click();
   await page.getByRole('navigation',{name:'Settings',exact:true}).waitFor();
  },
  async act({page,mark,log}) {
   let baseline;
   for(const category of ['General','Providers','Plugins','Cloud','Devices']) {
    await mark(`${category}: one title baseline and 28px content gap`);
    if(width<600) {
     const back=page.locator('.topbar').getByRole('button',{name:'Back to Settings',exact:true});
     if(await back.count()) await back.click();
    }
    const reopen=page.getByRole('button',{name:'Show sidebar',exact:true});
    if(width>=768 && await reopen.isVisible()) await reopen.click();
    await page.getByRole('navigation',{name:'Settings',exact:true}).getByRole('button',{name:category,exact:true}).click();
    const heading=shown(page.getByRole('heading',{name:category,exact:true,level:1}));
    await heading.waitFor();
    const geometry=await heading.evaluate(h=>{
     const title=h.closest('.st-page-title'), r=h.getBoundingClientRect(), style=getComputedStyle(h);
     const content=title.nextElementSibling || title.closest('.st-lcol')?.querySelector('.st-lscroll > *');
     return {x:r.x,y:r.y,height:r.height,font:style.fontSize,line:style.lineHeight,gap:content.getBoundingClientRect().y-title.getBoundingClientRect().bottom,overflow:document.documentElement.scrollWidth>innerWidth};
    });
    log(JSON.stringify({theme,width,category,...geometry}));
    baseline ??= geometry;
    expect(geometry.y===baseline.y && geometry.height===baseline.height,'count and no-count titles share a baseline');
    expect(geometry.font==='24px' && geometry.line==='30px','one Settings title scale');
    expect(geometry.gap===28,'every Settings page has one title-to-content gap');
    expect(!geometry.overflow,'no horizontal overflow');
    if(width<600) expect(geometry.x===16,'stacked titles, with or without a trailing action, sit on the 16px page gutter');
    if(category==='General' && width<600) {
     for(const label of ['Theme','Theme style']) {
      const row=page.locator('.st-row').filter({has:page.getByText(label,{exact:true})});
      const gap=await row.evaluate(el=>el.querySelector('.st-rc').getBoundingClientRect().top-el.querySelector('.st-rh').getBoundingClientRect().bottom);
      expect(gap>=8 && gap<=12,`${label} control sits below its complete helper on phone`);
     }
    }
   }
   if(width>=768) {
    await mark('persistent rail; collapse and reopen without an empty top band');
    const nav=page.getByRole('navigation',{name:'Workspace',exact:true});
    expect(await nav.isVisible(),'desktop rail is visible at medium widths');
    await nav.getByRole('button',{name:'Agents',exact:true}).click();
    await shown(page.getByRole('button',{name:/Dev \(Claude Code\)/})).click();
    const heading=page.getByRole('heading',{name:'Dev (Claude Code)',exact:true});
    await heading.waitFor();
    // Detail headings start at 53px at 1440; PageTitle centres its 30px h1 in a 32px row (54px).
    expect((await heading.boundingBox()).y===53,'detail title keeps its 1440px baseline at medium widths');
    expect(await page.locator('.topbar').count()===0,'no empty full-width top bar');
    if(await page.getByRole('button',{name:'Show sidebar',exact:true}).isVisible()) await page.getByRole('button',{name:'Show sidebar',exact:true}).click();
    await shown(page.getByRole('button',{name:'Hide sidebar',exact:true})).click();
    expect(await nav.isVisible(),'collapsed panel preserves destination switching');
    await page.getByRole('button',{name:'Show sidebar',exact:true}).click();
    expect(await page.getByPlaceholder('Search agents').isVisible(),'reopen restores panel controls');
    await mark('chat pages show one panel toggle: Hide in the open panel header, Show only while collapsed');
    await nav.getByRole('button',{name:'Chats',exact:true}).click();
    await page.locator('.p-head').filter({visible:true}).first().waitFor();
    const visible=name=>page.getByRole('button',{name,exact:true}).filter({visible:true}).count();
    expect(await visible('Hide sidebar')===1 && await visible('Show sidebar')===0,'the open Chats panel and the chat bar never both draw a panel toggle');
    await shown(page.getByRole('button',{name:'Hide sidebar',exact:true})).click();
    expect(await visible('Show sidebar')===1 && await visible('Hide sidebar')===0,'the collapsed panel keeps one reopen control');
    await shown(page.getByRole('button',{name:'Show sidebar',exact:true})).click();
    await page.locator('.p-head').filter({visible:true}).first().waitFor();
   }
  }
 }))) });
