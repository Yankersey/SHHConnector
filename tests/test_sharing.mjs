import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context=vm.createContext({console,setTimeout,clearTimeout,setInterval,clearInterval,TextDecoder,TextEncoder,performance,structuredClone,queueMicrotask,navigator:{userAgent:'node-test',platform:'Linux'}});
context.self=context;context.window=context;
for(const path of ['vendor/xterm.js','vendor/addon-serialize.js','mouse-filter.js','terminal-view.js','sharing.js'])vm.runInContext(fs.readFileSync(new URL('../static/'+path,import.meta.url),'utf8'),context);
const flush=term=>new Promise(resolve=>term.write('',resolve));
function frame(text,seq,snapshot=false){
  const bytes=new TextEncoder().encode(text),out=new Uint8Array(bytes.length+9),header=new DataView(out.buffer);
  header.setUint8(0,snapshot?1:0);header.setUint32(1,Math.floor(seq/4294967296));header.setUint32(5,seq%4294967296);out.set(bytes,9);return out.buffer;
}
function client(){
  const term=new context.Terminal({cols:40,rows:24,scrollback:20000,allowProposedApi:true});
  const serializer=new context.SerializeAddon.SerializeAddon();term.loadAddon(serializer);
  let filter=new context.TerminalMouseFilter(false,{keepScrollback:true});const sent=[],roles=[];
  const sharing=new context.TerminalSharing(term,serializer,{filter:()=>filter,send:message=>sent.push(message),reset:()=>{filter=new context.TerminalMouseFilter(false,{keepScrollback:true});},geometry(){},role:(data,replaying)=>roles.push({control:data.control,replaying})});
  sharing.message({type:'replay_start',cols:40,rows:24});sharing.message({type:'replay_end',seq:0});sharing.message({type:'sharing',control:true});
  return {term,serializer,sharing,sent,roles,get filter(){return filter;}};
}
const source=client();await flush(source.term);
const output='\x1b[?1049h\x1b[2J\x1b[H'+Array.from({length:700},(_,i)=>'\x1b[32m第 '+i+' 行 │ 中文 café é 🙂\x1b[0m\r\n').join('');
source.sharing.output(frame(output,1));await flush(source.term);
source.sharing.message({type:'snapshot_request',nonce:'fixture',seq:1,cols:40,rows:24});await flush(source.term);
const snapshot=source.sent.find(message=>message.type==='snapshot');assert.ok(snapshot);assert.equal(snapshot.seq,1);assert.ok(snapshot.text.includes('中文'));
const viewer=client();await flush(viewer.term);
viewer.sharing.message({type:'replay_start',cols:40,rows:24});viewer.sharing.output(frame(snapshot.text,1,true));viewer.sharing.message({type:'replay_end',seq:1});viewer.sharing.message({type:'sharing',control:false});await flush(viewer.term);
assert.equal(context.TerminalView.history(viewer.term),context.TerminalView.history(source.term));
for(const index of [0,100,699]){
  const a=source.term.buffer.normal.getLine(index),b=viewer.term.buffer.normal.getLine(index);assert.equal(a.translateToString(true),b.translateToString(true));assert.equal(a.getCell(0).getFgColor(),b.getCell(0).getFgColor());
}
assert.equal(viewer.roles.at(-1).control,false);assert.equal(viewer.roles.at(-1).replaying,false);
// Apply a controller's size change in stream order before the next frame.
for(const client of [source,viewer]){
  client.sharing.output(frame('改变尺寸前的输出\r\n',2));
  client.sharing.message({type:'geometry',seq:3,cols:28,rows:18});
  client.sharing.output(frame('手机接管后的中文🙂\r\n',4));
  client.sharing.message({type:'geometry',seq:5,cols:30,rows:20});
  client.sharing.output(frame('\x1b[31m红色界面',6));
  while(client.sharing.resizing)await client.sharing.resizing;
  await flush(client.term);
}
assert.equal(viewer.term.cols,30);assert.equal(viewer.term.rows,20);
assert.equal(context.TerminalView.history(viewer.term),context.TerminalView.history(source.term));
assert.equal(source.sharing.seq,6);assert.equal(viewer.sharing.seq,6);
assert.match(context.TerminalView.history(viewer.term),/改变尺寸前的输出\n手机接管后的中文🙂\n红色界面/);
assert.ok(!context.TerminalView.history(viewer.term).includes('\ufffd'));
assert.ok(viewer.sent.some(message=>message.type==='ack'));
source.term.dispose();viewer.term.dispose();

// A viewer can join while screen's cursor is still at the bottom of Codex's
// upper scroll region. Replaying margins must not move it to the first row.
const controller=client(),reader=client();
await flush(controller.term);
controller.sharing.output(frame('\x1b[?1000;1006h\x1b[1;4r甲\r\n乙\r\n丙\r\n丁\x1b[24;1H固定输入区\x1b[4;8H',1));
controller.sharing.message({type:'snapshot_request',nonce:'margin',seq:1,cols:40,rows:24});
await flush(controller.term);
const marginSnapshot=controller.sent.find(message=>message.nonce==='margin');
assert.ok(marginSnapshot);
reader.sharing.message({type:'replay_start',cols:40,rows:24});
reader.sharing.output(frame(marginSnapshot.text,1,true));
await flush(reader.term);
assert.equal(reader.term.buffer.active.cursorX,7);
assert.equal(reader.term.buffer.active.cursorY,3);
assert.equal(reader.filter.wheel(true,3,3,20,20),'\x1b[<64;3;3M');
assert.equal(reader.term.modes.mouseTrackingMode,'none');
for(const client of [controller,reader]){
  client.sharing.output(frame('\r\n新回复',2));
  await flush(client.term);
}
assert.equal(context.TerminalView.history(reader.term),context.TerminalView.history(controller.term));
controller.term.dispose();reader.term.dispose();
console.log('PASS shared terminal Unicode/color snapshot, ordered resize, replay and readonly role');
