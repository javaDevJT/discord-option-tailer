#!/usr/bin/env bun
// Exact-app operator helper. No generic RPC/config/command CLI is exposed.
// Secret bytes travel only in fd3 memory and authenticated pinned-CA TLS.
import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { request } from 'node:https';
import { randomBytes, createHash } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const APP = 'discord-option-tailer';
export const ORIGIN = 'https://192.168.0.2:8443';
export const DIRECTORY = '/mnt/Lake/discord-option-tailer-openwoof';
export const HOST_KEY = `${DIRECTORY}/openwoof-ingest-key`;
export const CONTAINER_KEY = '/run/secrets/openwoof-ingest-key';
export const KEYCHAIN_SERVICE = 'openwoof/openwoof.javadevjt.tech/truenas-192-168-0-2/discord-option-tailer/production';
export const KEYCHAIN_ACCOUNT = 'discord-option-tailer-collector';
const HERE = fileURLToPath(new URL('.', import.meta.url));
const ROOT = resolve(HERE, '..');
const PRIVATE = resolve(ROOT, 'artifacts/private/openwoof-deployment');
const RECEIPT = resolve(PRIVATE, 'custody.json');
const HELPER = resolve(PRIVATE, 'openwoof-keychain');
const CA = readFileSync(resolve(HERE, 'truenas-ca.pem'));
const TLS = {ca: CA, serverName: 'localhost', rejectUnauthorized: true};
export const TELEMETRY_ENV = {
  RELAY_OTEL_ENABLED: 'true',
  OTEL_EXPORTER_OTLP_ENDPOINT: 'http://127.0.0.1:4318',
  OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf',
  OPENWOOF_INGEST_KEY_FILE: CONTAINER_KEY,
};
export class SafeFailure extends Error {
  constructor(phase, jobId, errno) { super(phase); this.phase=phase; this.jobId=jobId; this.errno=errno; }
}
const fail = (phase, jobId) => { throw new SafeFailure(phase, jobId); };

export async function privateSecret(mode, executable=HELPER) {
  if (!['read-nas-to-fd','prompt-ingest-to-fd','read-ingest-to-fd','--test-ipc'].includes(mode)) fail('private-helper-mode');
  const child=spawn(executable,[mode],{stdio:['ignore','pipe','pipe','pipe']});
  const chunks=[];let total=0,stdout='',stderrBytes=0;
  const data=new Promise((accept,reject)=>{
    child.stdio[3].on('data',b=>{total+=b.length;if(total>4096){child.kill();reject(new SafeFailure('private-pipe-size'));}else chunks.push(b);});
    child.stdio[3].on('end',accept);child.stdio[3].on('error',()=>reject(new SafeFailure('private-pipe')));
  });
  child.stdout.on('data',b=>{if(stdout.length<4096)stdout+=b.toString();else child.kill();});
  child.stderr.on('data',b=>{stderrBytes+=b.length;});
  const code=await new Promise((accept,reject)=>{child.on('exit',accept);child.on('error',()=>reject(new SafeFailure('keychain-helper-launch')));});
  await data;
  const bytes=Buffer.concat(chunks);chunks.forEach(b=>b.fill(0));
  if(code!==0||stderrBytes||!bytes.length){bytes.fill(0);fail('keychain-helper-unavailable-or-cancelled');}
  try {const s=JSON.parse(stdout);if(s.ok!==true||typeof s.mode!=='string')throw Error();}
  catch{bytes.fill(0);fail('keychain-helper-status');}
  return bytes;
}

export async function connect(onNotification) {
  if(typeof Bun==='undefined')fail('operator-runtime-requires-bun');
  // Reuse the established TrueNAS advisor's operator credential mechanism.
  // stdout is a private child pipe here, never a tool result or console stream.
  const child=spawn('/usr/bin/security',['find-generic-password','-s','codex-truenas-mcp','-a','joshuaterk','-w','/Users/joshuaterk/Library/Keychains/login.keychain-db'],{stdio:['ignore','pipe','pipe']});
  const chunks=[];let size=0;
  child.stdout.on('data',b=>{size+=b.length;if(size>4096)child.kill();else chunks.push(b);});
  child.stderr.on('data',()=>{});
  const exit=await new Promise((accept,reject)=>{child.on('exit',accept);child.on('error',()=>reject(new SafeFailure('nas-keychain-reader')));});
  const value=Buffer.concat(chunks);chunks.forEach(b=>b.fill(0));
  if(exit!==0||!value.length||size>4096){value.fill(0);fail('nas-keychain-unavailable');}
  const key=value[value.length-1]===10?Buffer.from(value.subarray(0,-1)):Buffer.from(value);value.fill(0);
  const ws=new WebSocket('wss://192.168.0.2:8443/api/current',{tls:TLS});
  const pending=new Map();let next=0;
  const call=(method,params=[])=>new Promise((accept,reject)=>{
    if(ws.readyState!==1){reject(new SafeFailure('nas-connection-not-open-'+method));return;}
    const id=++next,timer=setTimeout(()=>{pending.delete(id);reject(new SafeFailure('rpc-timeout-'+method));},30000);
    pending.set(id,{accept,reject,timer,method});ws.send(JSON.stringify({jsonrpc:'2.0',id,method,params}));
  });
  ws.onmessage=e=>{let m;try{m=JSON.parse(String(e.data));}catch{return;}
    const p=pending.get(m.id);if(!p){if(m.method)try{onNotification?.(m);}catch{}return;}
    pending.delete(m.id);clearTimeout(p.timer);
    m.error?p.reject(new SafeFailure(`rpc-${p.method}`,undefined,m.error.data?.errno??m.error.data?.error)):p.accept(m.result);
  };
  try {
    await new Promise((accept,reject)=>{
      const timer=setTimeout(()=>reject(new SafeFailure('nas-tls-connect-timeout')),15000);
      ws.onopen=()=>{clearTimeout(timer);accept();};
      ws.onerror=()=>{clearTimeout(timer);reject(new SafeFailure('nas-tls'));};
    });
    if(await call('auth.login_with_api_key',[key.toString('utf8')])!==true)fail('nas-auth');
  }catch(e){ws.close();throw e;}finally{key.fill(0);}
  return {call,close:()=>ws.close()};
}

function saveReceipt(value) {
  mkdirSync(PRIVATE,{recursive:true,mode:0o700});
  writeFileSync(RECEIPT,JSON.stringify(value,null,2)+'\n',{mode:0o600});
}
function receipt() {
  const r=JSON.parse(readFileSync(RECEIPT,'utf8'));
  if(r.app!==APP||r.path!==HOST_KEY||r.keychainService!==KEYCHAIN_SERVICE||r.keychainAccount!==KEYCHAIN_ACCOUNT)fail('custody-receipt');
  return r;
}
export function fingerprint(config) {
  const canonical=x=>Array.isArray(x)?x.map(canonical):x&&typeof x==='object'?Object.fromEntries(Object.keys(x).sort().map(k=>[k,canonical(x[k])])):x;
  return createHash('sha256').update(JSON.stringify(canonical(config))).digest('hex');
}
export function imageReference(image) {
  if(!/^ghcr\.io\/javadevjt\/discord-option-tailer@sha256:[a-f0-9]{64}$/.test(image))fail('image-reference');
  return image;
}
export function makeConfig(before,image) {
  imageReference(image);
  const c=structuredClone(before),s=c.services?.relay;
  if(!s||Object.keys(c.services).length!==1||!s.environment||Array.isArray(s.environment)||s.env_file||s.entrypoint||s.command||!Array.isArray(s.volumes))fail('compose-preflight');
  if(s.network_mode&&!['default','bridge'].includes(s.network_mode))fail('shared-network-namespace');
  for(const key of Object.keys(s.environment)) {
    if(key.startsWith('OTEL_EXPORTER_OTLP_')||['OTEL_SERVICE_NAME','OTEL_RESOURCE_ATTRIBUTES','OPENWOOF_INGEST_KEY','OPENWOOF_INGEST_KEY_FILE'].includes(key))delete s.environment[key];
  }
  Object.assign(s.environment,TELEMETRY_ENV);
  const mount=`${HOST_KEY}:${CONTAINER_KEY}:ro`;
  const existing=s.volumes.filter(x=>typeof x==='string'?x.split(':')[1]===CONTAINER_KEY:x?.target===CONTAINER_KEY);
  if(existing.length&&!(existing.length===1&&existing[0]===mount))fail('foreign-secret-mount');
  if(!existing.length)s.volumes.push(mount);
  s.image=image;
  return c;
}
async function appState(client) {
  const app=await client.call('app.get_instance',[APP]);
  if(app.id!==APP||!app.custom_app||app.state!=='RUNNING')fail('app-preflight');
  const config=await client.call('app.config',[APP]);
  if(!config.services?.relay||Object.keys(config.services).length!==1)fail('app-service-preflight');
  return {app,config};
}
export function secureFile(s,a,empty=false) {
  return s?.realpath===HOST_KEY&&s.type==='FILE'&&s.uid===0&&s.gid===0&&(s.mode&0o777)===0o600&&a?.path===HOST_KEY&&a.trivial===true&&(empty?s.size===0:s.size>0&&s.size<=4096);
}
async function fileState(client) {
  return {stat:await client.call('filesystem.stat',[HOST_KEY]),acl:await client.call('filesystem.getacl',[HOST_KEY])};
}
async function job(client,id) {
  if(!Number.isSafeInteger(id))fail('job-id');
  for(let n=0;n<120;n++){
    const j=await client.call('core.get_jobs',[[['id','=',id]],{get:true}]);
    if(j.state==='SUCCESS')return {id,state:j.state};
    if(['FAILED','ABORTED'].includes(j.state))fail('job-failed-inspect-before-retry',id);
    await Bun.sleep(5000);
  }fail('job-timeout-inspect-before-retry',id);
}
export function multipart(bytes,boundary=randomBytes(18).toString('hex')) {
  const metadata=JSON.stringify({method:'filesystem.put',params:[HOST_KEY,{mode:null,append:false}]});
  const body=Buffer.concat([Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="data"\r\nContent-Type: application/json\r\n\r\n${metadata}\r\n`),Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="openwoof-ingest-key"\r\nContent-Type: application/octet-stream\r\n\r\n`),bytes,Buffer.from(`\r\n--${boundary}--\r\n`)]);
  return {body,boundary};
}
async function upload(client,bytes) {
  const token=await client.call('auth.generate_token',[120,{},true,true]);
  const url=new URL('/_upload/',ORIGIN);url.searchParams.set('auth_token',token);
  const {body,boundary}=multipart(bytes);
  try {
    const result=await new Promise((accept,reject)=>{
      const req=request(url,{method:'POST',ca:CA,servername:'localhost',rejectUnauthorized:true,agent:false,headers:{'content-type':`multipart/form-data; boundary=${boundary}`,'content-length':body.length}},res=>{
        let text='',size=0;
        if(res.statusCode<200||res.statusCode>=300){res.resume();reject(new SafeFailure('upload-http'));return;}
        res.on('data',b=>{size+=b.length;if(size>65536){res.destroy();reject(new SafeFailure('upload-response-size'));}else text+=b.toString();});
        res.on('end',()=>{try{accept(JSON.parse(text));}catch{reject(new SafeFailure('upload-response'));}});
        res.on('error',()=>reject(new SafeFailure('upload-response')));
      });req.setTimeout(30000,()=>req.destroy());req.on('error',()=>reject(new SafeFailure('upload-transport')));req.end(body);
    });
    return await job(client,result.job_id??result.jobId??result);
  }finally{body.fill(0);}
}
export async function prepare(client) {
  await appState(client);
  if(existsSync(RECEIPT))fail('custody-already-recorded-inspect-before-retry');
  const parent=await client.call('filesystem.stat',['/mnt/Lake']);
  const acl=await client.call('filesystem.getacl',['/mnt/Lake']);
  if(parent.uid!==0||parent.type!=='DIRECTORY'||(parent.mode&0o022)!==0||acl.trivial!==true)fail('secret-parent-security');
  try{await client.call('filesystem.stat',[DIRECTORY]);fail('existing-secret-directory');}
  catch(e){if(!(e instanceof SafeFailure)||e.errno!==2)throw e;}
  const r={app:APP,path:HOST_KEY,keychainService:KEYCHAIN_SERVICE,keychainAccount:KEYCHAIN_ACCOUNT,phase:'prepare-submitted',at:new Date().toISOString()};
  saveReceipt(r); // No blind repeat after an uncertain filesystem operation.
  await client.call('filesystem.mkdir',[{path:DIRECTORY,options:{mode:'700'}}]);
  await job(client,await client.call('filesystem.setperm',[{path:DIRECTORY,uid:0,gid:0,mode:'700',options:{recursive:false,traverse:false,stripacl:true}}]));
  await upload(client,Buffer.alloc(0));
  await job(client,await client.call('filesystem.setperm',[{path:HOST_KEY,uid:0,gid:0,mode:'600',options:{recursive:false,traverse:false,stripacl:true}}]));
  const f=await fileState(client);if(!secureFile(f.stat,f.acl,true))fail('empty-file-security');
  const next={...r,phase:'prepared-empty',inode:f.stat.inode,device:f.stat.dev};saveReceipt(next);return next;
}
export async function deliveryPreflight(client) {
  await appState(client);const r=receipt(),f=await fileState(client);
  if(r.phase!=='prepared-empty'||!secureFile(f.stat,f.acl,true)||r.inode!==f.stat.inode||r.device!==f.stat.dev)fail('prepared-file-drift');
  return r;
}
export async function deliver(client,key) {
  const r=await deliveryPreflight(client);
  if(!Buffer.isBuffer(key)||!key.length||key.length>4096)fail('private-key-buffer');
  const attempt={...r,phase:'delivery-submitted',at:new Date().toISOString()};saveReceipt(attempt);
  try{await upload(client,key);}finally{key.fill(0);}
  const after=await fileState(client);if(!secureFile(after.stat,after.acl))fail('delivered-file-security');
  const next={...r,phase:'delivered',at:new Date().toISOString()};saveReceipt(next);return next;
}
export async function deploy(client,image,proof,storage={load:receipt,save:saveReceipt}) {
  imageReference(image);
  if(proof.image!==image||proof.ci_test_build_scan_publish_succeeded!==true||proof.publication_receipt_matches_scan!==true||proof.security_high_critical_findings!==0)fail('release-proof');
  const r=storage.load(),f=await fileState(client);
  if(r.phase!=='delivered'||!secureFile(f.stat,f.acl))fail('secret-not-ready');
  const {config:before}=await appState(client),expected=makeConfig(before,image);
  const {config:second}=await appState(client);if(!isDeepStrictEqual(before,second))fail('app-config-drift');
  const attempt={...r,phase:'deploy-submitted',image,sourceRevision:proof.source_revision,expectedConfigHash:fingerprint(expected),at:new Date().toISOString()};
  storage.save(attempt);
  const id=await client.call('app.update',[APP,{custom_compose_config:expected}]);
  storage.save({...attempt,jobId:id});const completed=await job(client,id);
  for(let n=0;n<30;n++){
    const app=await client.call('app.get_instance',[APP]);
    if(app.state==='RUNNING'){
      const actual=await client.call('app.config',[APP]);
      const containers=app.active_workloads?.container_details??[];
      if(!isDeepStrictEqual(actual,expected))fail('post-deployment-config-drift',id);
      if(containers.length===1&&containers[0].state==='running'&&containers[0].image===image){
        storage.save({...attempt,phase:'deployed',jobId:id,containerId:containers[0].id});
        return {app:APP,state:app.state,job:completed,image,containerId:containers[0].id,configPreservedExceptImageAndTelemetry:true,secretReadOnlyMount:CONTAINER_KEY};
      }
    }
    await Bun.sleep(2000);
  }fail('runtime-not-ready-inspect-before-retry',id);
}

async function main() {
  const args=process.argv.slice(2),action=args[0];
  if(!['prepare','deliver','deploy'].includes(action)||args[1]!=='--confirm'||args[2]!==APP||args.length!==(action==='deploy'?5:3))fail('usage');
  let proof;
  if(action==='deploy'){
    imageReference(args[3]);proof=JSON.parse(readFileSync(args[4],'utf8'));
  }
  let key;
  if(action==='deliver'){
    const preflight=await connect();
    try{await deliveryPreflight(preflight);}finally{preflight.close();}
    // Human approval may take minutes. No NAS session is held open while the
    // hidden dialog runs. Fresh authentication and file checks follow it.
    key=await privateSecret('prompt-ingest-to-fd');
  }
  let client;
  try{
    client=await connect();
    const result=action==='prepare'?await prepare(client):action==='deliver'?await deliver(client,key):await deploy(client,args[3],proof);
    console.log(JSON.stringify(result));
  }finally{key?.fill(0);client?.close();}
}
if(import.meta.main)main().catch(e=>{console.error(JSON.stringify({app:APP,phase:e instanceof SafeFailure?e.phase:'operator-operation-failed',jobId:e instanceof SafeFailure?e.jobId:undefined}));process.exitCode=1;});
