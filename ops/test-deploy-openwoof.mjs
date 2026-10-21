import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { APP, HOST_KEY, CONTAINER_KEY, TELEMETRY_ENV, SafeFailure, makeConfig, imageReference, secureFile, multipart, deploy, fingerprint } from './deploy-openwoof.mjs';

const image='ghcr.io/javadevjt/discord-option-tailer@sha256:'+'a'.repeat(64);
const before={services:{relay:{image:'previous',environment:{DASHBOARD_PASSWORD:'fixture-original-password',RELAY_ROBINHOOD_REDIRECT_URI:'https://original.example/callback',HOME:'/home/relay',OTEL_EXPORTER_OTLP_HEADERS:'fixture-conflict'},volumes:['relay-data:/data'],ports:['192.168.0.2:8787:8080','127.0.0.1:8766:8766'],restart:'unless-stopped'}},volumes:{'relay-data':{}},networks:{default:{}}};
const stat={realpath:HOST_KEY,type:'FILE',uid:0,gid:0,mode:0o100600,size:32,inode:42,dev:2};
const acl={path:HOST_KEY,trivial:true};
const proof={image,source_revision:'b'.repeat(40),ci_test_build_scan_publish_succeeded:true,publication_receipt_matches_scan:true,security_high_critical_findings:0};

test('only image, telemetry env and exact readonly file mount change; input is not mutated',()=>{
  const original=structuredClone(before),after=makeConfig(before,image);
  assert.deepEqual(before,original);
  assert.equal(after.services.relay.environment.DASHBOARD_PASSWORD,before.services.relay.environment.DASHBOARD_PASSWORD);
  assert.equal(after.services.relay.environment.RELAY_ROBINHOOD_REDIRECT_URI,before.services.relay.environment.RELAY_ROBINHOOD_REDIRECT_URI);
  assert.equal(after.services.relay.environment.OTEL_EXPORTER_OTLP_HEADERS,undefined);
  assert.deepEqual(after.services.relay.volumes,['relay-data:/data',`${HOST_KEY}:${CONTAINER_KEY}:ro`]);
  assert.deepEqual(after.services.relay.ports,before.services.relay.ports);
  assert.deepEqual(after.volumes,before.volumes);assert.deepEqual(after.networks,before.networks);
  const restored=structuredClone(after);restored.services.relay.image=before.services.relay.image;
  restored.services.relay.environment=before.services.relay.environment;restored.services.relay.volumes=before.services.relay.volumes;
  assert.deepEqual(restored,before);
});
test('matching telemetry configuration is idempotent',()=>{
  const a=makeConfig(before,image);assert.deepEqual(makeConfig(a,image),a);
});
test('rejects unreviewable service overrides and foreign mounts',()=>{
  for(const change of [c=>c.services.extra={},c=>c.services.relay.command=['sh'],c=>c.services.relay.env_file='hidden',c=>c.services.relay.network_mode='host',c=>c.services.relay.volumes.push('/foreign:'+CONTAINER_KEY+':ro')]){
    const c=structuredClone(before);change(c);assert.throws(()=>makeConfig(c,image),SafeFailure);
  }
});
test('only the exact app immutable GHCR image is accepted',()=>{
  for(const value of [image+' ',image.replace('tailer','other'),'ghcr.io/javadevjt/discord-option-tailer:latest','https://'+image])assert.throws(()=>imageReference(value),SafeFailure);
  assert.equal(imageReference(image),image);
});
test('secret path must be owner-only regular file, with trivial ACL and exact realpath',()=>{
  assert.ok(secureFile(stat,acl));
  for(const patch of [{realpath:'/elsewhere'},{type:'SYMLINK'},{uid:1001},{gid:1001},{mode:0o100644},{size:0},{size:4097}])assert.equal(secureFile({...stat,...patch},acl),false);
  assert.equal(secureFile(stat,{...acl,trivial:false}),false);assert.ok(secureFile({...stat,size:0},acl,true));
});
test('upload metadata fixes path, no append and no permission broadening',()=>{
  const {body}=multipart(Buffer.from('fixture-secret'),'boundary');
  assert.ok(body.includes(Buffer.from(JSON.stringify({method:'filesystem.put',params:[HOST_KEY,{mode:null,append:false}]}))));
  body.fill(0);assert.ok(body.every(x=>x===0));
});
test('SDK receives a loopback endpoint and file path, never an upstream credential',()=>{
  assert.equal(TELEMETRY_ENV.OTEL_EXPORTER_OTLP_ENDPOINT,'http://127.0.0.1:4318');
  assert.equal(TELEMETRY_ENV.OPENWOOF_INGEST_KEY_FILE,CONTAINER_KEY);
  assert.equal(Object.keys(TELEMETRY_ENV).some(x=>/HEADERS|^OPENWOOF_INGEST_KEY$/.test(x)),false);
});

function fixture({updateFails=false,drift=false,jobFails=false}={}){
  const calls=[],saved=[];let configReads=0,updated=false;
  const client={async call(method,params){calls.push({method,params});
    if(method==='filesystem.stat')return stat;
    if(method==='filesystem.getacl')return acl;
    if(method==='app.get_instance')return {id:APP,custom_app:true,state:'RUNNING',active_workloads:{container_details:[{id:'fixture-container',image:updated?image:'previous',state:'running'}]}};
    if(method==='app.config'){
      configReads++;return updated?makeConfig(before,image):drift&&configReads===2?{...before,unexpected:'drift'}:structuredClone(before);
    }
    if(method==='app.update'){assert.equal(params[0],APP);assert.deepEqual(params[1],{custom_compose_config:makeConfig(before,image)});if(updateFails)throw new SafeFailure('rpc-timeout');updated=true;return 7;}
    if(method==='core.get_jobs')return {state:jobFails?'FAILED':'SUCCESS'};
    throw Error('unexpected RPC '+method);
  }};
  return {client,calls,saved,storage:{load:()=>({app:APP,path:HOST_KEY,phase:'delivered'}),save:x=>saved.push(structuredClone(x))}};
}
test('one exact update after drift check; full expected config and running image verified',async()=>{
  const f=fixture(),result=await deploy(f.client,image,proof,f.storage);
  assert.equal(result.job.state,'SUCCESS');assert.equal(result.image,image);
  assert.equal(f.calls.filter(x=>x.method==='app.update').length,1);
  assert.deepEqual(f.saved.map(x=>x.phase),['deploy-submitted','deploy-submitted','deployed']);
  assert.equal(f.saved.at(-1).expectedConfigHash,fingerprint(makeConfig(before,image)));
});
test('uncertain update is journaled and never automatically resubmitted',async()=>{
  const f=fixture({updateFails:true});await assert.rejects(deploy(f.client,image,proof,f.storage),/rpc-timeout/);
  assert.equal(f.calls.filter(x=>x.method==='app.update').length,1);assert.equal(f.saved.at(-1).phase,'deploy-submitted');
});
test('config drift stops before any update; failed job has no rollback or retry',async()=>{
  const d=fixture({drift:true});await assert.rejects(deploy(d.client,image,proof,d.storage),/app-config-drift/);assert.equal(d.calls.some(x=>x.method==='app.update'),false);
  const j=fixture({jobFails:true});await assert.rejects(deploy(j.client,image,proof,j.storage),/job-failed-inspect-before-retry/);assert.equal(j.calls.filter(x=>x.method==='app.update').length,1);
});
test('bad security or publication proof cannot access NAS mutation',async()=>{
  for(const patch of [{security_high_critical_findings:1},{image:image+'x'},{ci_test_build_scan_publish_succeeded:false},{publication_receipt_matches_scan:false}]){
    const f=fixture();await assert.rejects(deploy(f.client,image,{...proof,...patch},f.storage),/release-proof/);assert.equal(f.calls.length,0);
  }
});
test('Swift helper has exact app custody and no Keychain overwrite path',()=>{
  const text=readFileSync(new URL('./openwoof-keychain.swift',import.meta.url),'utf8');
  assert.ok(text.includes('openwoof/openwoof.javadevjt.tech/truenas-192-168-0-2/discord-option-tailer/production'));
  assert.ok(text.includes('discord-option-tailer-collector'));
  assert.ok(text.includes('requirePrivatePipe()'));assert.ok(text.includes('SecItemAdd('));
  assert.equal(/SecItemUpdate\(|SecItemDelete\(|print\(.*secret/.test(text),false);
});
