/** Account access is a replaceable transport; signatures and local builds do not depend on it. */
import { createHash, randomBytes } from 'node:crypto';
import { promises as fs } from 'node:fs';
import { spawn } from 'node:child_process';
import path from 'node:path';
import os from 'node:os';
export const SOURCE_ORIGIN = 'https://hobbycoders.com';

async function credential(root:string,write?:string):Promise<string> {
  const file=path.join(root,'update-account.dat');
  if(write===undefined){try{await fs.access(file);}catch{return '';}}
  const script=path.resolve(__dirname,'../../scripts/update-credential.ps1');
  return new Promise((resolve,reject)=>{
    const child=spawn('powershell.exe',['-NoProfile','-NonInteractive','-File',script,'-Action',write===undefined?'read':'write','-Path',file],{windowsHide:true,stdio:['pipe','pipe','pipe'],timeout:15000});
    let text='';child.stdout.on('data',chunk=>{text+=chunk;if(text.length>256)child.kill();});
    child.once('error',()=>reject(new Error('Could not access the Windows-protected update credential')));
    child.once('exit',code=>code===0?resolve(text):reject(new Error('Could not access the Windows-protected update credential')));
    child.stdin.end(write??'');
  });
}
async function call(endpoint:string,token?:string,body?:unknown):Promise<Response> {
  return fetch(SOURCE_ORIGIN+endpoint,{method:body?'POST':'GET',redirect:'error',signal:AbortSignal.timeout(120000),headers:{...(token?{Authorization:'Bearer '+token}:{}),...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});
}
async function signIn(root:string):Promise<string> {
  const token=randomBytes(32).toString('hex');
  const response=await call('/v1/desktop/logins',undefined,{token_hash:createHash('sha256').update(token).digest('hex'),device_name:os.hostname().slice(0,80)||'Windows desktop'});
  if(!response.ok)throw new Error(`Desktop account sign-in is unavailable (${response.status})`);
  const flow=await response.json() as {request_id:string;authorization_url:string};
  if(!/^[A-Za-z0-9_-]{43}$/.test(flow.request_id)||flow.authorization_url!==SOURCE_ORIGIN+'/browser/desktop?request_id='+flow.request_id)throw new Error('Invalid account sign-in response');
  await new Promise<void>((resolve,reject)=>{const child=spawn('rundll32.exe',['url.dll,FileProtocolHandler',flow.authorization_url],{windowsHide:true,stdio:'ignore'});child.once('error',reject);child.once('exit',code=>code===0?resolve():reject(new Error('Could not open account sign-in')));});
  const end=Date.now()+15*60_000;
  while(Date.now()<end){
    await new Promise(resolve=>setTimeout(resolve,3000));
    const poll=await call('/v1/desktop/logins/'+flow.request_id,token);
    if(!poll.ok)throw new Error('Account sign-in expired or was cancelled. Try again.');
    if((await poll.json() as {status:string}).status==='approved'){await credential(root,token);return token;}
  }
  throw new Error('Finish account sign-in and check for updates again.');
}
export async function accountSourceFetch(root:string,url:string,interactive:boolean):Promise<Response> {
  const target=new URL(url);
  if(target.origin!==SOURCE_ORIGIN||!/^\/v1\/desktop\/(manifest|sources\/[a-f0-9]{40})$/.test(target.pathname)||target.search||target.hash)throw new Error('Invalid source download endpoint');
  let token=await credential(root);
  if(!token){if(!interactive)throw new Error('Sign in to get updates. Click Check for updates to open account sign-in.');token=await signIn(root);}
  let result=await call(target.pathname,token);
  if(result.status===401&&interactive){token=await signIn(root);result=await call(target.pathname,token);}
  if(result.status===401)throw new Error('Your update sign-in expired. Click Check for updates to sign in again.');
  return result;
}
