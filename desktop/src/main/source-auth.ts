/** Public hub downloads. Signed manifests establish trust; no account or key is needed. */
export const SOURCE_ORIGIN = 'https://hobbycoders.com';

/** Refuse redirects and non-update URLs; never inherit browser credentials. */
export async function publicSourceFetch(url:string):Promise<Response> {
 const target=new URL(url);
 if(target.origin!==SOURCE_ORIGIN||target.username||target.password||!/^\/v1\/desktop\/(manifest|manifest\/(?:nightly|edge)|sources\/[a-f0-9]{40})$/.test(target.pathname)||target.search||target.hash)throw new Error('Invalid source download endpoint');
 return fetch(target.href,{method:'GET',redirect:'error',credentials:'omit',signal:AbortSignal.timeout(120000)});
}
