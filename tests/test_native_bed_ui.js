// Device-free execution of the actual temporary panel script, not Safari QA.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
class Element {
  constructor(){this.children=[];this.style={setProperty(key,value){this[key]=value;}};this.attributes={};this.listeners={};this.disabled=true;}
  append(child){this.children.push(child);}
  replaceChildren(){this.children=[];}
  setAttribute(key,value){this.attributes[key]=value;}
  addEventListener(key,fn){this.listeners[key]=fn;}
}
const nodes=new Map();
const document={activeElement:null,createElement:()=>new Element(),getElementById:id=>{
  if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);
}};
const controls={piano:[0,1,.01,.5],piano_tone:[0,1,.01,1],master:[0,1,.01,0],
  osc1:[0,1,.01,.13],osc2:[0,1,.01,.1],cutoff:[20,20000,1,487],wet:[0,1,.01,.74],
  bed_level:[0,1,.01,1],bed_key:[0,11,1,-1],bed_rise:[0,60,.5,0],bed_rise_cutoff:[200,20000,1,3000],
  bed_mellow:[0,1,1,0],bed_mellow_cutoff:[100,8000,1,400],bed_fade:[0,1,1,0],bed_release:[0,1,1,0]};
let state={stale:false,exited:null,pending:0,error:null,values:Object.fromEntries(Object.entries(controls).map(([k,v])=>[k,v[3]])),
  status:{fault:0,routed:true,frames:512,blocks:100,bed_mask:129,bed_key:7,active_beds:1}};
const sent=[];
const actions=[];
const fetch=async(url,args)=>({ok:true,json:async()=>{
  if(url==='/controls')return controls;
  if(url==='/status')return state;
  if(url!=='/control'){actions.push({url,body:JSON.parse(args.body),epoch:args.headers['X-Stave-Epoch']});return {message:'Accepted'};}
  sent.push(JSON.parse(args.body));return {queued:sent.length};
}});
const html=fs.readFileSync(path.join(__dirname,'../native_v2/audition.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const context=vm.createContext({document,fetch,AbortSignal,confirm:()=>true,setTimeout:()=>{}});
vm.runInContext(script,context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await flush();await flush();
  assert.deepEqual(nodes.get('mixer').children.map(n=>n.attributes['data-control']),['osc1','osc2','piano','cutoff','wet','master']);
  for(const label of nodes.get('mixer').children){const input=label.children.at(-1);assert.equal(input.attributes['aria-orientation'],'vertical');assert.match(input.style['--fill'],/%$/);}
  assert.equal(nodes.get('tone-controls').children[0].attributes['data-control'],'piano_tone');
  assert.equal(sent.length,0); // Rendering saved levels never edits the sound.
  for(const value of [20,100,487,8000,20000])assert.equal(vm.runInContext(`fromSlider('cutoff',toSlider('cutoff',${value}))`,context),value);
  assert.equal(vm.runInContext("fromSlider('cutoff',0)",context),20);
  assert.equal(vm.runInContext("fromSlider('cutoff',1000)",context),20000);
  const filter=nodes.get('mixer').children[3].children.at(-1);filter.value=500;filter.listeners.input();await flush();
  assert.deepEqual(sent.at(-1),{key:'cutoff',value:632});assert.equal(filter.attributes['aria-valuetext'],'632 Hz');
  const keys=nodes.get('bed-keys').children;
  assert.equal(keys.length,12);
  keys.forEach((button,key)=>assert.equal(button.disabled,![0,7].includes(key)));
  assert.equal(keys[7].attributes['aria-pressed'],'true');
  assert.match(nodes.get('bed-status').textContent,/Selected G/);
  keys[0].onclick();await flush();assert.deepEqual(sent.at(-1),{key:'bed_key',value:0});
  nodes.get('bed-stop').onclick();await flush();assert.deepEqual(sent.at(-1),{key:'bed_release',value:1});
  state={...state,stale:true};await vm.runInContext('poll()',context);
  assert.ok(keys.every(b=>b.disabled));
  for(const id of ['bed-in','bed-out','bed-stop'])assert.ok(nodes.get(id).disabled);
  const count=sent.length;
  state={...state,stale:false,status:{...state.status,bed_mask:0,bed_key:-1,active_beds:0}};
  await vm.runInContext('poll()',context);
  assert.equal(sent.length,count);assert.ok(keys.every(b=>b.disabled));
  assert.match(nodes.get('bed-status').textContent,/No recorded pads loaded/);
  for(const label of nodes.get('bed-controls').children)assert.ok(label.children.at(-1).disabled);
  nodes.get('nav-system').onclick();assert.equal(nodes.get('system-panel').hidden,false);assert.equal(nodes.get('stage-panel').hidden,true);
  state={...state,epoch:'session-a',runtime:{persistence:true,can_restart:true,restoring:false,devices:{midi:['Keys:midi'],audio:['USB:l','USB:r']},midi_source:'Keys:midi',audio_left:'USB:l',audio_right:'USB:r'}};
  await vm.runInContext('poll()',context);
  assert.equal(nodes.get('save').disabled,false);assert.equal(nodes.get('route-picker').hidden,false);
  nodes.get('save').onclick();await flush();assert.equal(actions.at(-1).url,'/save');assert.equal(actions.at(-1).epoch,'session-a');
  nodes.get('apply-routes').onclick();await flush();assert.deepEqual(actions.at(-1).body,{midi_source:'Keys:midi',audio_left:'USB:l',audio_right:'USB:r'});
  state={...state,epoch:'session-b',runtime:{...state.runtime,restoring:true}};
  await vm.runInContext('poll()',context);assert.equal(nodes.get('mute').disabled,true);assert.equal(nodes.get('save').disabled,true);
  const actionCount=actions.length;
  state={...state,runtime:{...state.runtime,restoring:false}};
  await vm.runInContext('poll()',context);assert.equal(actions.length,actionCount);assert.equal(sent.length,count);
  console.log('PASS: loaded/absent bed keys, actions, stale recovery, Stage/Edit/System, save and route epochs, no reconnect replay');
})().catch(error=>{console.error(error);process.exitCode=1;});
