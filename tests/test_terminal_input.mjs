import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Exercise the exact bundled encoders without opening an app or browser.
// Expose webpack's loader in this test context only.
let bundle=fs.readFileSync(new URL('../static/vendor/xterm.js',import.meta.url),'utf8');
function moduleFor(symbol){
  const index=bundle.indexOf('t.'+symbol);
  assert.ok(index>0,symbol);
  const modules=[...bundle.slice(0,index).matchAll(/(?:^|},)(\d+)\([^)]*\)\{/g)];
  return Number(modules.at(-1)[1]);
}
const mouseModule=moduleFor('MouseService');
bundle=bundle.replace('return function i(s){','globalThis.xtermRequire=function i(s){')
  .replace('o.exports}(6081)','o.exports};return globalThis.xtermRequire(6081)');
const context=vm.createContext({
  console,setTimeout,clearTimeout,setInterval,clearInterval,TextDecoder,TextEncoder,
  structuredClone,performance,queueMicrotask,
  WheelEvent:{DOM_DELTA_PIXEL:0,DOM_DELTA_LINE:1,DOM_DELTA_PAGE:2},
  navigator:{userAgent:'input-tests',platform:'Linux'}
});
context.self=context;context.window=context;
vm.runInContext(bundle,context);
for(const path of ['terminal-input.js','terminal-view.js','mouse-filter.js']){
  vm.runInContext(fs.readFileSync(new URL('../static/'+path,import.meta.url),'utf8'),context);
}
const {MouseService}=context.xtermRequire(mouseModule);
const term=new context.Terminal({cols:160,rows:30,allowProposedApi:true});
const filter=new context.TerminalMouseFilter(true,{keepScrollback:true});
const messages=[];
context.TerminalInput.install(term,message=>messages.push(message));
const core=term._core;
const mouse=new MouseService(
  {dimensions:{device:{cell:{height:20}}}},
  {getMouseReportCoords:event=>({col:event.col,row:event.row,x:event.col*10,y:event.row*20})},
  core.mouseStateService,core.coreService,core._bufferService,core.optionsService,
  {shouldForceSelection:event=>event.shiftKey},core._logService,{dpr:1}
);
const ctx={target:{screenElement:{},element:{}},focus(){},requestedEvents:{}};
function event(type,values={}){
  return {
    type,col:120,row:8,deltaY:0,deltaMode:1,buttons:0,button:0,
    shiftKey:false,ctrlKey:false,altKey:false,metaKey:false,
    preventDefault(){},stopPropagation(){},...values
  };
}
const write=text=>new Promise(resolve=>term.write(filter.feed(text),resolve));
function key(key,keyCode,shiftKey=false){
  const result=core._keyboardService.evaluateKeyDown(event('keydown',{key,keyCode,shiftKey}));
  if(result.key)core.input(result.key,true);
  return result.key;
}
core.input('草稿写了一半',true);
assert.equal(messages.pop().data,'草稿写了一半');
mouse._handleMouseDown(ctx,event('mousedown'));
mouse._handleMouseMove(ctx,event('mousemove'));
mouse._handlePassiveWheel(ctx,event('wheel',{deltaY:-1}));
assert.equal(messages.length,0); // a shell does not receive mouse coordinates
await write(Array.from({length:80},(_,i)=>'回复 '+i+'\r\n').join(''));
let localWheel;
const localScroll=context.TerminalView.installScroll(term,{
  addEventListener(name,handler){if(name==='wheel')localWheel=handler;},
  removeEventListener(){}
},{
  remoteMouse:()=>term.modes.mouseTrackingMode!=='none',historyOpen:()=>false,
  hasArchive:()=>false,lineHeight:()=>20,changed(){},live(){},openHistory(){}
});
localWheel(event('wheel',{deltaY:-4}));
assert.ok(term.buffer.normal.viewportY<term.buffer.normal.baseY);
assert.equal(messages.length,0); // looking back never edits the partial draft
assert.equal(localScroll.key(event('keydown',{key:'ArrowLeft',shiftKey:true})),false);
localScroll.dispose();

await write('\x1b[?1h\x1b[?1000h');
mouse._handleMouseMove(ctx,event('mousemove'));
assert.equal(messages.length,0);
mouse._handleWheel(ctx,event('wheel',{deltaY:-1}));
assert.equal(messages.length,1);
assert.equal(messages[0].binary,true);
assert.deepEqual([...messages.pop().data].map(c=>c.charCodeAt(0)),[27,91,77,96,153,41]);
// Legacy column 121 is byte 153, never clamped or UTF-8 encoded here.
mouse._handleMouseDown(ctx,event('mousedown'));
assert.deepEqual([...messages.pop().data].map(c=>c.charCodeAt(0)),[27,91,77,32,153,41]);

await write('\x1b[?1006h');
mouse._handleWheel(ctx,event('wheel',{deltaY:-1}));
assert.equal(messages.pop().data,'\x1b[<64;121;9M');
mouse._handleMouseDown(ctx,event('mousedown'));
assert.equal(messages.pop().data,'\x1b[<0;121;9M');
mouse._handleMouseMove(ctx,event('mousemove'));
assert.equal(messages.length,0);
mouse._handleMouseDown(ctx,event('mousedown',{shiftKey:true}));
assert.equal(messages.length,0); // local text selection
assert.equal(key('ArrowLeft',37),'\x1bOD');
assert.equal(messages.pop().data,'\x1bOD');
assert.equal(key('ArrowRight',39,true),'\x1b[1;2C');
assert.equal(messages.pop().data,'\x1b[1;2C');
assert.equal(key('ArrowLeft',37,true),'\x1b[1;2D');
assert.equal(messages.pop().data,'\x1b[1;2D');
core.input('继续打字',true);
assert.equal(messages.pop().data,'继续打字');

// Turning off mouse support must take effect before the next gesture.
await write('\x1b[?1000l\x1b[?1006l\x1b[?1l');
mouse._handleMouseDown(ctx,event('mousedown'));
mouse._handleMouseMove(ctx,event('mousemove'));
mouse._handlePassiveWheel(ctx,event('wheel',{deltaY:-1}));
assert.equal(messages.length,0);
assert.equal(key('ArrowLeft',37),'\x1b[D');
messages.length=0;
// A read-only viewer cannot send any mouse or keyboard input.
await write('\x1b[?1003;1006h');
term.options.disableStdin=true;
core.input('只读',true);
mouse._handleWheel(ctx,event('wheel',{deltaY:-1}));
mouse._handleMouseMove(ctx,event('mousemove'));
assert.equal(messages.length,0);
const eventsArgument=process.argv.indexOf('--events-file');
if(eventsArgument!==-1){
  const cases={};
  term.options.disableStdin=false;
  for(const encoding of ['legacy','sgr']){
    if(process.argv.includes('--encoding')&&process.argv[process.argv.indexOf('--encoding')+1]!==encoding)continue;
    const startupArgument=process.argv.indexOf('--startup-file');
    await write('\x1bc'+(startupArgument===-1?
      '\x1b[?1h\x1b[?1000h'+(encoding==='sgr'?'\x1b[?1006h':''):
      fs.readFileSync(process.argv[startupArgument+1],'utf8')));
    messages.length=0;
    core.input('abc',true);
    key('ArrowLeft',37);
    key('ArrowLeft',37,true);
    core.input('Z',true);
    mouse._handleWheel(ctx,event('wheel',{deltaY:-1}));
    mouse._handleMouseDown(ctx,event('mousedown'));
    mouse._handleMouseMove(ctx,event('mousemove'));
    key('ArrowRight',39,true);
    key('ArrowLeft',37,true);
    key('ArrowRight',39);
    key('ArrowLeft',37);
    core.input('中文',true);
    core.input('\x04',true);
    cases[encoding]=JSON.parse(JSON.stringify(messages));
  }
  fs.writeFileSync(process.argv[eventsArgument+1],JSON.stringify(cases));
}
term.dispose();
console.log('PASS native xterm keyboard, Unicode draft, mouse negotiation, binary coordinates, selection and readonly input');
