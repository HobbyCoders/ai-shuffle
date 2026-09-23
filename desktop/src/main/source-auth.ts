/** Public download transport. Signed manifests establish trust without accounts. */
export const SOURCE_ORIGIN = 'https://hobbycoders.com';
/** The hub's license gate (android/hub/src/license-gate.ts) reads the lease from this header. */
export const LEASE_HEADER = 'X-Shuffle-Lease';
/** A lease header value: the signed envelope as base64url, within the hub's 4 KiB limit. */
export function isLeaseValue(value: unknown): value is string {
 return typeof value === 'string' && value.length > 0 && value.length <= 4096 && /^[A-Za-z0-9_-]+$/.test(value);
}
/**
 * First install only: before any worker has run there is no lease, so the hub's
 * gate accepts the license key the owner entered instead, in this header.
 */
export const LICENSE_KEY_HEADER = 'X-Shuffle-License-Key';
/** A license key as typed (ASH-XXXXX-XXXXX-XXXXX-XXXXX): the shape the hub's gate accepts. */
export function isLicenseKeyValue(value: unknown): value is string {
 return typeof value === 'string' && /^[A-Za-z0-9 -]{20,64}$/.test(value) && value.trim() === value;
}
/**
 * GET a manifest or archive from the hub. A lease or license key, when given,
 * is attached only after the URL is checked to be the hub's own update
 * endpoint; redirects are refused, so the header can never be carried anywhere
 * else. A lease wins over a key; the key is sent only when there is no lease.
 */
export async function publicSourceFetch(url:string, lease?:string, licenseKey?:string):Promise<Response> {
 const target=new URL(url);
 if(target.origin!==SOURCE_ORIGIN||target.username||target.password||!/^\/v1\/desktop\/(manifest|manifest\/edge|sources\/[a-f0-9]{40})$/.test(target.pathname)||target.search||target.hash)throw new Error('Invalid source download endpoint');
 const init:RequestInit={method:'GET',redirect:'error',credentials:'omit',signal:AbortSignal.timeout(120000)};
 if(lease!==undefined){if(!isLeaseValue(lease))throw new Error('Invalid license lease');init.headers={[LEASE_HEADER]:lease};}
 else if(licenseKey!==undefined){if(!isLicenseKeyValue(licenseKey))throw new Error('Invalid license key');init.headers={[LICENSE_KEY_HEADER]:licenseKey};}
 return fetch(target.href,init);
}
