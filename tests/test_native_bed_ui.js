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
  getAttribute(key){return this.attributes[key];}
  addEventListener(key,fn){this.listeners[key]=fn;}
  focus(){document.activeElement=this;}
  getBoundingClientRect(){return {height:208,width:208};}
  setPointerCapture(id){this.capture=id;}
  hasPointerCapture(id){return this.capture===id;}
  releasePointerCapture(){this.capture=null;}
  closest(selector){return selector==='[data-map-key]'&&this.attributes['data-map-key']?this:null;}
}
const nodes=new Map();
const presetNodes=Array.from({length:5},()=>new Element());
const documentListeners={};const bodyClasses=new Set();
const document={activeElement:null,body:{classList:{toggle(name,on){if(on)bodyClasses.add(name);else bodyClasses.delete(name);}}},addEventListener(key,fn){documentListeners[key]=fn;},createElement:()=>new Element(),querySelectorAll:selector=>{
  if(selector==='[data-preset]')return presetNodes;
  if(selector==='[data-map-key]'){
    const result=[];const visit=node=>{if(node.attributes?.['data-map-key'])result.push(node);for(const child of node.children||[])visit(child);};
    for(const node of nodes.values())visit(node);return [...new Set(result)];
  }
  return [];
},getElementById:id=>{
  if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);
}};
for(let i=0;i<3;i++)document.getElementById('transpose-stepper').append(new Element());
const controls={piano:[0,1,.01,.5],piano_tone:[0,1,.01,1],master:[0,1,.01,0],
  osc1:[0,1,.01,.13],osc2:[0,1,.01,.1],cutoff:[20,20000,1,487],cutoff_min:[20,20000,1,20],cutoff_max:[20,20000,1,20000],wet:[0,1,.01,.74],
  shimmer:[0,1,1,0],wave1:[0,4,1,0],wave2:[0,4,1,2],attack:[0,10000,10,530],release:[0,30000,10,530],resonance:[.5,10,.01,.7],
  delay_wet:[0,1,.01,0],delay_feedback:[0,.99,.01,.35],piano_room:[0,1,.01,.4],piano_reverb:[0,1,.01,.13],reverb:[0,6,1,0],shimmer_mix:[0,1,.01,.07],freeze:[0,1,1,0],
  bed_level:[0,1,.01,1],bed_key:[0,11,1,-1],bed_rise:[0,60,.5,0],bed_rise_cutoff:[200,20000,1,3000],
  bed_mellow:[0,1,1,0],bed_mellow_cutoff:[100,8000,1,400],bed_fade:[0,1,1,0],bed_release:[0,1,1,0]};
for(const n of [1,2])Object.assign(controls,{['attack'+n]:[0,10000,1,530],['decay'+n]:[0,20000,1,1500],['sustain'+n]:[0,100,.1,80],['release'+n]:[0,30000,1,530]});
Object.assign(controls,{envelope_link:[0,1,1,0],volume_link:[0,1,1,0],master_low:[-6,6,.1,0],master_mid:[-6,6,.1,0],master_high:[-6,6,.1,0],master_lowcut:[0,1,1,0],master_lowcut_hz:[20,200,1,80]});
Object.assign(controls,{piano_room_size:[0,1,.01,.5],piano_room_damp:[0,.99,.01,.6],reverb_decay:[0,30,.1,6],reverb_predelay:[0,150,1,25],reverb_lowcut:[20,20000,1,80],reverb_highcut:[20,20000,1,7000],reverb_damp:[0,.99,.01,.5],delay_time:[1,1000,1,500],delay_lowcut:[20,1000,1,20],delay_highcut:[500,20000,1,18000]});
Object.assign(controls,{transpose:[-24,24,1,0],piano_octave:[-3,3,1,0],octave1:[-3,3,1,0],octave2:[-3,3,1,0],record_start:[0,1,1,0],record_stop:[0,1,1,0],release_all:[0,1,1,0]});
Object.assign(controls,{pan1:[-1,1,.01,0],pan2:[-1,1,.01,0],detune:[0,1,.001,.07],spread:[0,1,.001,.85]});
Object.assign(controls,{piano_lowcut:[20,500,1,20],piano_velocity:[1,4,.01,1],osc1_reverb:[0,1,.01,1],osc2_reverb:[0,1,.01,1],piano_delay:[0,1,.01,0],filter_slope:[0,1,1,0],piano_filter:[0,1,1,0],bpm:[40,240,1,120],delay_division:[0,8,1,3]});
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
const context=vm.createContext({document,window:{addEventListener(){}},fetch,AbortSignal,confirm:()=>true,prompt:()=>null,setTimeout:()=>{}});
vm.runInContext(script,context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await flush();await flush();
  assert.deepEqual(nodes.get('mixer').children.map(n=>n.attributes['data-control']),['osc1','osc2','piano','cutoff','wet','master']);
  for(const label of nodes.get('mixer').children){const input=label.children.find(x=>x.type==='range');assert.equal(input.attributes['aria-orientation'],'vertical');assert.match(input.style['--fill'],/%$/);}
  assert.equal(nodes.get('tone-controls').children[0].attributes['data-control'],'piano_tone');
  assert.equal(sent.length,0); // Rendering saved levels never edits the sound.
  for(const value of [20,100,487,8000,20000])assert.equal(vm.runInContext(`fromSlider('cutoff',toSlider('cutoff',${value}))`,context),value);
  assert.equal(vm.runInContext("fromSlider('cutoff',0)",context),20);
  assert.equal(vm.runInContext("fromSlider('cutoff',1000)",context),20000);
  assert.equal(nodes.get('edit-osc').children.slice(0,2).map(x=>x.attributes['data-control']).join(','),'cutoff_min,cutoff_max');
  const filter=nodes.get('mixer').children[3].children.find(x=>x.type==='range');filter.value=500;filter.listeners.input();await flush();
  assert.deepEqual(sent.at(-1),{key:'cutoff',value:632},nodes.get('notice').textContent);assert.equal(filter.attributes['aria-valuetext'],'632 Hz');
  const filterMin=nodes.get('edit-osc').children[0].children.find(x=>x.type==='range');
  filterMin.value=vm.runInContext("toSlider('cutoff_min',300)",context);filterMin.listeners.input();await flush();
  assert.deepEqual(sent.at(-1),{key:'cutoff_min',value:300});assert.equal(vm.runInContext("fromSlider('cutoff',0)",context),300,'Stage filter bottom follows the saved sweep minimum');
  const fx=nodes.get('mixer').children[4].children.find(x=>x.type==='range');
  const pointer=(type,y,id=1,x=20)=>{let prevented=false;fx.listeners[type]({button:0,pointerId:id,clientY:y,clientX:x,preventDefault(){prevented=true;}});return prevented;};
  let before=sent.length;
  assert.ok(pointer('pointerdown',200));pointer('pointerup',200);await flush();
  assert.equal(Number(fx.value),.74);assert.equal(sent.length,before,'tap anywhere does not change FX');
  assert.equal(nodes.get('mixer').children[4].attributes['data-active'],'false','released pointer clears active highlight');
  pointer('pointerdown',200);pointer('pointermove',200);assert.equal(sent.length,before,'stationary touch sends nothing');
  pointer('pointermove',180,2);assert.equal(sent.length,before,'another finger cannot hijack this fader');
  pointer('pointermove',180);await flush();assert.deepEqual(sent.at(-1),{key:'wet',value:.84});
  await vm.runInContext('poll()',context);assert.equal(Number(fx.value),.84,'ack poll must not move a held fader');
  pointer('pointermove',-1000);await flush();assert.equal(Number(fx.value),1,'drag clamps to range');
  pointer('pointercancel',-1000);before=sent.length;pointer('pointermove',200);await flush();assert.equal(sent.length,before,'cancel terminates gesture');
  await vm.runInContext('poll()',context);assert.equal(Number(fx.value),.74,'released focused control follows authoritative state');
  const tone=nodes.get('tone-controls').children[0].children.find(x=>x.type==='range');
  tone.listeners.pointerdown({button:0,pointerId:3,clientX:100,clientY:50,preventDefault(){}});
  tone.listeners.pointermove({pointerId:3,clientX:80,clientY:50,preventDefault(){}});await flush();
  assert.deepEqual(sent.at(-1),{key:'piano_tone',value:.9},'horizontal pickup moves relative to current value');
  tone.listeners.pointerup({pointerId:3});
  pointer('pointerdown',100);state={...state,epoch:'new-gesture-session'};await vm.runInContext('poll()',context);
  before=sent.length;pointer('pointermove',80);await flush();assert.equal(sent.length,before,'epoch change cancels old gesture');
  pointer('pointerdown',100);nodes.get('nav-edit').onclick();pointer('pointermove',80);await flush();assert.equal(sent.length,before,'changing view cancels gesture');
  assert.equal(nodes.get('edit-wave1').children[0].attributes['data-control'],'wave1');
  assert.equal(nodes.get('edit-wave2').children[0].attributes['data-control'],'wave2');
  assert.equal(nodes.get('edit-osc').children[2].attributes['data-control'],'resonance');
  assert.equal(nodes.get('edit-osc').children[0].attributes['data-advanced'],'true','filter calibration is available without crowding the default editor');
  assert.ok(!bodyClasses.has('show-advanced'));
  nodes.get('sound-advanced').onclick();assert.ok(bodyClasses.has('show-advanced'));assert.equal(nodes.get('system-advanced').attributes['aria-pressed'],'true');
  nodes.get('system-advanced').onclick();assert.ok(!bodyClasses.has('show-advanced'));
  nodes.get('fine-controls').onclick();assert.ok(bodyClasses.has('fine-controls'));assert.equal(nodes.get('fine-controls').textContent,'FINE KNOBS');
  assert.deepEqual(nodes.get('edit-osc').children.slice(-8).map(x=>x.attributes['data-control']),['pan1','pan2','detune','spread','osc1_reverb','osc2_reverb','filter_slope','piano_filter']);
  for(const n of [1,2]){
    assert.deepEqual(nodes.get('edit-env'+n).children.map(x=>x.attributes['data-control']),['attack','decay','sustain','release'].map(x=>x+n));
    assert.ok(!nodes.get('envelope'+n).attributes.points.includes('NaN'));
    for(const [key,max] of [['attack',10000],['decay',20000],['release',30000]])for(const v of [0,1,70,530,max])assert.equal(vm.runInContext(`fromSlider('${key+n}',toSlider('${key+n}',${v}))`,context),v);
  }
  assert.deepEqual(nodes.get('edit-link').children.map(x=>x.attributes['data-control']),['envelope_link','volume_link']);
  nodes.get('volume-link-stage').onclick();await flush();
  assert.deepEqual(sent.at(-1),{key:'volume_link',value:1});assert.equal(nodes.get('volume-link-stage').attributes['aria-pressed'],'true');
  const osc1Fader=nodes.get('mixer').children[0].children.find(x=>x.type==='range');
  const osc2Fader=nodes.get('mixer').children[1].children.find(x=>x.type==='range');
  osc1Fader.listeners.pointerdown({button:0,pointerId:22,clientY:200,clientX:10,preventDefault(){}});
  osc1Fader.listeners.pointermove({pointerId:22,clientY:180,clientX:10,preventDefault(){}});await flush();
  assert.equal(Number(osc1Fader.value),.23);assert.equal(Number(osc2Fader.value),.2,'linked OSC faders preserve their .03 balance immediately');
  assert.deepEqual(sent.at(-1),{key:'osc1',value:.23},'one acknowledged control moves the linked pair');
  osc1Fader.listeners.pointerup({pointerId:22});
  assert.equal(nodes.get('edit-global').children.length,5);
  assert.deepEqual(nodes.get('edit-piano').children.slice(-5).map(x=>x.attributes['data-control']),['piano_room_size','piano_room_damp','piano_lowcut','piano_velocity','piano_delay']);
  assert.deepEqual(nodes.get('edit-reverb').children.slice(-5).map(x=>x.attributes['data-control']),['reverb_decay','reverb_predelay','reverb_lowcut','reverb_highcut','reverb_damp']);
  assert.deepEqual(nodes.get('edit-delay').children.slice(-5).map(x=>x.attributes['data-control']),['delay_time','delay_lowcut','delay_highcut','bpm','delay_division']);
  for(const [key,values] of Object.entries({reverb_lowcut:[20,80,1000,20000],reverb_highcut:[20,7000,20000],delay_lowcut:[20,100,1000],delay_highcut:[500,18000,20000]}))for(const value of values)assert.equal(vm.runInContext(`fromSlider('${key}',toSlider('${key}',${value}))`,context),value);
  assert.equal(nodes.get('edit-osc').children[2].className.includes('knob-control'),true);
  assert.match(nodes.get('edit-osc').children[2].children.at(-2).style['--knob-turn'],/deg$/);
  assert.equal(nodes.get('edit-delay').children[0].attributes['data-control'],'delay_wet');
  assert.equal(nodes.get('edit-piano').children[0].attributes['data-control'],'piano_room');
  nodes.get('sound-tab-piano').onclick();assert.equal(nodes.get('sound-piano').attributes['data-open'],'true');assert.equal(nodes.get('sound-osc').attributes['data-open'],'false');
  assert.equal(nodes.get('sound-tab-piano').attributes['aria-pressed'],'true');
  nodes.get('shimmer-toggle').onclick();await flush();assert.deepEqual(sent.at(-1),{key:'shimmer',value:1});
  const keys=nodes.get('bed-keys').children;
  assert.equal(keys.length,12);
  keys.forEach((button,key)=>assert.equal(button.disabled,![0,7].includes(key)));
  assert.equal(keys[7].attributes['aria-pressed'],'true');
  assert.match(nodes.get('bed-status').textContent,/G · playing/);
  keys[0].onclick();await flush();assert.deepEqual(sent.at(-1),{key:'bed_key',value:0});
  nodes.get('bed-stop').onclick();await flush();assert.deepEqual(sent.at(-1),{key:'bed_release',value:1});
  state={...state,stale:true};await vm.runInContext('poll()',context);
  assert.ok(keys.every(b=>b.disabled));
  assert.ok(nodes.get('bed-stop').disabled);
  const count=sent.length;
  state={...state,stale:false,status:{...state.status,bed_mask:0,bed_key:-1,active_beds:0}};
  await vm.runInContext('poll()',context);
  assert.equal(sent.length,count);assert.ok(keys.every(b=>b.disabled));
  assert.match(nodes.get('bed-status').textContent,/No pads loaded/);
  for(const label of nodes.get('bed-controls').children)assert.equal(label.children.at(-1).disabled,false,'bed shaping can be prepared before recordings exist');
  nodes.get('nav-system').onclick();assert.equal(nodes.get('system-panel').hidden,false);assert.equal(nodes.get('stage-panel').hidden,true);
  state={...state,epoch:'session-a',runtime:{persistence:true,can_restart:true,restoring:false,devices:{midi:['Keys:midi'],audio:['USB:l','USB:r']},midi_source:'Keys:midi',audio_left:'USB:l',audio_right:'USB:r'}};
  await vm.runInContext('poll()',context);
  assert.equal(nodes.get('save').disabled,false);assert.equal(nodes.get('route-picker').hidden,false);
  assert.equal(nodes.get('midi-map').disabled,false);
  nodes.get('midi-map').onclick();assert.ok(bodyClasses.has('midi-map-mode'));assert.equal(nodes.get('midi-map-banner').hidden,false);
  const osc1=nodes.get('mixer').children[0];
  documentListeners.pointerdown({target:osc1,preventDefault(){},stopImmediatePropagation(){}});
  assert.equal(osc1.attributes['data-midi-selected'],'true');
  state={...state,status:{...state.status,midi_cc_serial:1,midi_cc:23,midi_cc_value:99}};
  await vm.runInContext('poll()',context);await flush();
  assert.deepEqual(actions.at(-1).body,{key:'osc1',cc:23});assert.equal(actions.at(-1).url,'/midi-map');
  const mappedRaw=Array(Object.keys(controls).length).fill(-1),mappedSerial=Array(Object.keys(controls).length).fill(0),osc1Index=Object.keys(controls).indexOf('osc1');
  mappedRaw[osc1Index]=112;mappedSerial[osc1Index]=1;state={...state,values:{...state.values,osc1:.88},status:{...state.status,midi_mapped_raw:mappedRaw,midi_mapped_serial:mappedSerial},runtime:{...state.runtime,midi_maps:{osc1:23}}};
  await vm.runInContext('poll()',context);assert.equal(Number(osc1Fader.value),.88,'mapped hardware telemetry repaints the authoritative value');
  assert.equal(osc1.attributes['data-midi-cc'],'23');assert.equal(nodes.get('midi-map-list').children.length,1,'system view summarizes saved hardware mappings');
  nodes.get('midi-map-done').onclick();assert.ok(!bodyClasses.has('midi-map-mode'));assert.equal(nodes.get('midi-map-banner').hidden,true);
  nodes.get('save').onclick();await flush();assert.equal(actions.at(-1).url,'/save');assert.equal(actions.at(-1).epoch,'session-a');
  nodes.get('apply-routes').onclick();await flush();assert.deepEqual(actions.at(-1).body,{midi_source:'Keys:midi',audio_left:'USB:l',audio_right:'USB:r'});
  state={...state,epoch:'session-b',runtime:{...state.runtime,restoring:true}};
  await vm.runInContext('poll()',context);assert.equal(nodes.get('mute').disabled,true);assert.equal(nodes.get('save').disabled,true);
  const actionCount=actions.length;
  state={...state,runtime:{...state.runtime,restoring:false}};
  await vm.runInContext('poll()',context);assert.equal(actions.length,actionCount);assert.equal(sent.length,count);
  state={...state,stale:true,status:{},runtime:{...state.runtime,preparation:true,restoring:true}};
  await vm.runInContext('poll()',context);
  assert.equal(nodes.get('connection').textContent,'Connected');
  assert.doesNotMatch(nodes.get('notice').textContent,/Prepare|preparation/i);
  assert.equal(nodes.get('audio-status').textContent,'AUDIO —');
  assert.equal(nodes.get('save').disabled,false);
  assert.equal(fx.disabled,false);
  assert.equal(nodes.get('edit-osc').children[0].children.find(x=>x.type==='range').disabled,false);
  assert.equal(nodes.get('mixer').children[5].children.find(x=>x.type==='range').disabled,true);
  assert.equal(nodes.get('freeze-toggle').disabled,true);
  assert.ok(keys.every(b=>b.disabled));
  before=sent.length;
  await vm.runInContext("send('master',1);send('bed_key',0);send('freeze',1)",context);
  assert.equal(sent.length,before,'no performance actions can be queued for reconnection');
  pointer('pointerdown',200);pointer('pointermove',180);pointer('pointerup',180);await flush();
  assert.equal(sent.at(-1).key,'wet','tone controls work without MIDI or audio hardware');
  nodes.get('save').onclick();await flush();assert.equal(actions.at(-1).url,'/save');
  state={...state,runtime:{...state.runtime,preparation:false}};
  await vm.runInContext('poll()',context);assert.equal(fx.disabled,true,'restore transition temporarily gates edits');
  console.log('PASS: relative pickup, gesture cancellation, state reconciliation, grouped controls, shimmer, bed actions, save/routes and recovery');
})().catch(error=>{console.error(error);process.exitCode=1;});
