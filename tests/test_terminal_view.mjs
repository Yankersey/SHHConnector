import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context=vm.createContext({});
vm.runInContext(fs.readFileSync(new URL('../static/terminal-view.js',import.meta.url),'utf8'),context);
const view=context.TerminalView;
assert.equal(view.resolveTheme('auto',false),'light');
assert.equal(view.resolveTheme('auto',true),'dark');
assert.equal(view.resolveTheme('light',true),'light');
assert.equal(view.resolveTheme('dark',false),'dark');
assert.equal(view.palette('light').background,'#ffffff');
assert.equal(view.palette('dark').background,'#000000');
const changed=view.palette('light');changed.background='#ff0000';assert.equal(view.palette('light').background,'#ffffff');

function terminal(lines){return {buffer:{normal:{length:lines.length,getLine:i=>lines[i]}}};}
function line(text,isWrapped=false){return {isWrapped,translateToString:trim=>trim?text.trimEnd():text};}
const unicode=terminal([line('中文 │ café é 🙂'),line('自动换行 '),line('继续',true),line(''),line('')]);
assert.equal(view.history(unicode),'中文 │ café é 🙂\n自动换行 继续');
assert.equal(view.history(unicode,'掉线前的中文'),'掉线前的中文\n中文 │ café é 🙂\n自动换行 继续');
assert.equal(view.history(terminal([])),'');
const many=Array.from({length:20003},(_,i)=>line('中文第 '+i+' 行'));
assert.ok(!view.history(terminal(many)).includes('中文第 2 行'));
assert.ok(view.history(terminal(many)).startsWith('中文第 3 行'));
assert.ok(view.history(terminal([line('新连接')]),many.map((_,i)=>'旧记录 '+i).join('\n')).split('\n').length<=20000);
const bounded=view.history(terminal([line('🙂'+'中'.repeat(2097151))]));
assert.equal(bounded,'中'.repeat(2097151));

let wheel,removed=false,disposed=false,history=0,live=0,changes=0,remoteMouse=false,historyOpen=false;
const calls=[];
const term={rows:20,buffer:{active:{type:'normal',baseY:300,viewportY:300}},scrollLines:n=>calls.push(['lines',n]),scrollPages:n=>calls.push(['pages',n]),scrollToTop:()=>calls.push(['top']),onScroll:()=>({dispose:()=>{disposed=true;}})};
const element={addEventListener:(name,listener,options)=>{assert.equal(name,'wheel');assert.equal(options.capture,true);assert.equal(options.passive,false);wheel=listener;},removeEventListener:()=>{removed=true;}};
const controller=view.installScroll(term,element,{remoteMouse:()=>remoteMouse,historyOpen:()=>historyOpen,lineHeight:()=>20,openHistory:()=>{history++;},changed:()=>{changes++;},live:()=>{live++;}});
function event(values={}){return {deltaY:0,deltaMode:0,type:'keydown',shiftKey:false,ctrlKey:false,metaKey:false,altKey:false,preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;},...values};}
let e=event({deltaY:-80});wheel(e);assert.deepEqual(calls.pop(),['lines',-4]);assert.ok(e.prevented&&e.stopped);
wheel(event({deltaY:60}));assert.deepEqual(calls.pop(),['lines',3]);
wheel(event({deltaY:-3,deltaMode:1}));assert.deepEqual(calls.pop(),['lines',-3]);
wheel(event({deltaY:1,deltaMode:2}));assert.deepEqual(calls.pop(),['lines',20]);
const before=calls.length;wheel(event({deltaY:.5}));wheel(event({deltaY:.5}));assert.equal(calls.length,before);
term.buffer.active.type='alternate';e=event({deltaY:-60});wheel(e);assert.equal(history,1);assert.ok(e.prevented);assert.equal(calls.length,before);
e=event({deltaY:60});wheel(e);assert.ok(e.prevented);assert.equal(history,1);
remoteMouse=true;e=event({deltaY:-60});wheel(e);assert.ok(!e.prevented);assert.equal(history,1);
e=event({deltaY:-60,shiftKey:true});wheel(e);assert.equal(history,2);assert.ok(e.prevented);
historyOpen=true;e=event({deltaY:-60});wheel(e);assert.ok(!e.prevented);assert.equal(history,2);historyOpen=false;
remoteMouse=false;term.buffer.active.type='normal';
e=event({key:'PageUp',shiftKey:true});assert.ok(controller.key(e));assert.deepEqual(calls.pop(),['pages',-1]);assert.ok(e.prevented);
assert.ok(controller.key(event({key:'PageDown',shiftKey:true})));assert.deepEqual(calls.pop(),['pages',1]);
assert.ok(controller.key(event({key:'Home',shiftKey:true})));assert.deepEqual(calls.pop(),['top']);
assert.ok(controller.key(event({key:'End',shiftKey:true})));assert.equal(live,1);
assert.equal(controller.key(event({key:'PageUp'})),false);
assert.equal(controller.key(event({key:'PageUp',shiftKey:true,ctrlKey:true})),false);
assert.ok(changes>=6);
controller.dispose();assert.ok(removed&&disposed);

// Use the shipped renderer as well as mocks: screen enters an alternate
// display on attachment even when its remote shell is printing ordinary logs.
Object.assign(context,{console,setTimeout,clearTimeout,setInterval,clearInterval,TextDecoder,TextEncoder,performance,structuredClone,queueMicrotask,navigator:{userAgent:'node-tests',platform:'Linux'}});
context.self=context;context.window=context;
vm.runInContext(fs.readFileSync(new URL('../static/vendor/xterm.js',import.meta.url),'utf8'),context);
vm.runInContext(fs.readFileSync(new URL('../static/vendor/addon-unicode11.js',import.meta.url),'utf8'),context);
vm.runInContext(fs.readFileSync(new URL('../static/mouse-filter.js',import.meta.url),'utf8'),context);
const actual=new context.Terminal({cols:80,rows:24,scrollback:20000,allowProposedApi:true});
actual.loadAddon(new context.Unicode11Addon.Unicode11Addon());
actual.unicode.activeVersion='11';
const filter=new context.TerminalMouseFilter(false,{keepScrollback:true}),decoder=new TextDecoder();
const output='\x1b[?1049h\x1b[2J\x1b[H'+Array.from({length:300},(_,i)=>'\x1b[32m第 '+i+' 行 │ 中文 café é 🙂\x1b[0m\r\n').join('');
const encoded=new TextEncoder().encode(output);let rendered='';
for(let i=0;i<encoded.length;i+=7)rendered+=filter.feed(decoder.decode(encoded.slice(i,i+7),{stream:true}));
rendered+=filter.feed(decoder.decode());
await new Promise(resolve=>actual.write(rendered,resolve));
assert.equal(actual.buffer.active.type,'normal');
assert.ok(actual.buffer.normal.baseY>250);
const previous=view.history(actual);
assert.ok(previous.includes('第 0 行 │ 中文 café é 🙂'));
assert.ok(previous.includes('第 299 行 │ 中文 café é 🙂'));
assert.ok(!previous.includes('\ufffd'));
assert.ok(!previous.includes('\x1b'));
// A new transport resets terminal state, while the review snapshot retains
// previously received Unicode without contacting screen's lossy hardcopy path.
actual.reset();await new Promise(resolve=>actual.write('重新连接后的中文\r\n',resolve));
assert.equal(actual.unicode.activeVersion,'11');
const reconnected=view.history(actual,previous);
assert.ok(reconnected.includes('第 0 行 │ 中文 café é 🙂'));
assert.ok(reconnected.includes('重新连接后的中文'));
actual.dispose();

// GNU screen represents an upward scroll above Codex's composer as CSI M
// at the first row. A direct terminal can receive CSI S for the same scroll.
const write=(term,text)=>new Promise(resolve=>term.write(text,resolve));
const visible=term=>Array.from({length:term.rows},(_,row)=>{
  const buffer=term.buffer.active;
  return buffer.getLine(buffer.baseY+row).translateToString(false);
});
for(const command of ['\x1b[1;4r\x1b[1;5H\x1b[2M','\x1b[1;4r\x1b[8;5H\x1b[2S']){
  const fixed=new context.Terminal({cols:40,rows:8,scrollback:20});
  const baseline=new context.Terminal({cols:40,rows:8,scrollback:20});
  view.installScreenHistory(fixed);
  fixed.reset(); // A reconnect resets the renderer without removing the fix.
  const initial='\x1b[32m较早的回复 🙂\x1b[0m\r\n旧回复 B\r\n旧回复 C\r\n旧回复 D\x1b[8;1Hcomposer';
  for(const term of [fixed,baseline]){
    await write(term,initial+command.slice(0,-1));
    await write(term,command.slice(-1));
  }
  assert.deepEqual(visible(fixed),visible(baseline));
  assert.equal(fixed.buffer.active.cursorX,baseline.buffer.active.cursorX);
  assert.equal(fixed.buffer.active.cursorY,baseline.buffer.active.cursorY);
  assert.ok(!view.history(baseline).includes('较早的回复'));
  assert.equal(fixed.buffer.normal.baseY,2);
  assert.equal(fixed.buffer.normal.getLine(0).getCell(0).getFgColor(),2);
  assert.match(view.history(fixed),/^较早的回复 🙂\n旧回复 B\n旧回复 C/);
  // Headless xterm has no DOM viewport. Set the same scroll state directly.
  fixed._core._bufferService.scrollLines(-2);
  await write(fixed,'\x1b[1;1H\x1b[S');
  assert.equal(fixed.buffer.normal.viewportY,0);
  // A non-top scroll region and line edits below the first row stay local.
  await write(fixed,'\x1b[2;4r\x1b[S\x1b[r\x1b[2;1H\x1b[M');
  assert.equal(fixed.buffer.normal.baseY,3);
  await write(fixed,'\x1b[3J');
  assert.equal(fixed.buffer.normal.baseY,0);
  await write(fixed,'\x1b[?1049h\x1b[H\x1b[S\x1b[M\x1b[?1049l');
  assert.equal(fixed.buffer.normal.baseY,0);
  fixed.dispose();
  baseline.dispose();
}

// Screen sends a top-row DL for some full redraws. Repainting the same
// lines should leave one copy of the transcript and no old composer.
const redraw=new context.Terminal({cols:40,rows:8,scrollback:20});
view.installScreenHistory(redraw);
const frame='提问\r\n运行中的状态栏\r\n旧回复\x1b[8;1Hcomposer';
await write(redraw,frame+'\x1b[H\x1b[3M'+frame);
assert.equal(redraw.buffer.normal.baseY,0);
assert.equal(view.history(redraw).match(/运行中的状态栏/g).length,1);
redraw.reset();

const commit='\x1b[1;4r第一条回复\r\n第二条回复\r\n第三条回复\r\n第四条回复\r\n第五条回复\r\n第六条回复\x1b[r';
await write(redraw,commit);
assert.equal(redraw.buffer.normal.baseY,2);
await write(redraw,'\x1b[H\x1b[2J临时画面\x1b[H\x1b[2J');
assert.equal(redraw.buffer.normal.baseY,2); // ordinary redraws retain history
await write(redraw,'\x1b[H\x1b[2J'); // repeated ED 2 does not imply ED 3
assert.equal(redraw.buffer.normal.baseY,2);
assert.equal(view.history(redraw).match(/第一条回复/g).length,1);
await write(redraw,'\x1b[3J');
assert.equal(redraw.buffer.normal.baseY,0);
redraw.dispose();
console.log('PASS terminal Unicode history, theme and scroll interactions');
