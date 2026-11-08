import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context=vm.createContext({console});
vm.runInContext(fs.readFileSync(new URL('../static/workspace-sync.js',import.meta.url),'utf8'),context);
function client(){
  const views=new Map(),observed=[],removed=[];
  const sync=new context.WorkspaceSync({
    views:()=>views,apply(){},
    open:async record=>views.set(record.id,{record,connected:false}),
    remove:view=>{removed.push(view.record.id);views.delete(view.record.id);},
    connected:view=>view.connected,
    observe:async(view,live)=>{observed.push([view.record.id,live.connection_id]);view.connected=true;},
    render(){}
  });
  return {sync,views,observed,removed};
}
const record={id:'one',name:'电脑窗口',state:'connected',layout:{visible:true,x:120,width:800}};
let snapshot={sessions:[record],shared:{one:{connection_id:'live-one',forwarding:true}}};
const desktop=client(),phone=client();
await phone.sync.sync(snapshot);
assert.equal(phone.views.size,1);assert.equal(phone.observed.length,1);
assert.equal(phone.views.get('one').record.layout.width,800);
// The phone joins a running PTY once; snapshots never reconnect SSH.
await phone.sync.sync(snapshot);assert.equal(phone.observed.length,1);
phone.views.get('one').connected=false;
await phone.sync.sync(snapshot);assert.equal(phone.observed.length,1);
phone.sync.wake();await phone.sync.sync(snapshot);assert.equal(phone.observed.length,2);
// A window created from the phone is also restored on the desktop, same ID.
const second={id:'two',name:'手机新窗口',state:'connected',layout:{visible:true}};
snapshot={sessions:[record,second],shared:{...snapshot.shared,two:{connection_id:'live-two',forwarding:false}}};
await desktop.sync.sync(snapshot);await phone.sync.sync(snapshot);
assert.deepEqual([...desktop.views.keys()],[...phone.views.keys()]);
assert.equal(desktop.observed.length,2);
// Intentional per-page detach stays detached; it is not undone by polling.
phone.sync.pause('one');phone.views.delete('one');phone.sync.wake();
await phone.sync.sync(snapshot);assert.equal(phone.views.has('one'),false);
phone.sync.resume('one');await phone.sync.sync(snapshot);assert.equal(phone.views.has('one'),true);
// A saved screen without a live backend PTY is displayed, never auto-connected.
const saved={id:'saved',state:'detached',layout:{visible:true}};
await phone.sync.sync({sessions:[record,second,saved],shared:snapshot.shared});
assert.equal(phone.views.has('saved'),true);assert.equal(phone.views.get('saved').connected,false);
assert.equal(phone.observed.some(([id])=>id==='saved'),false);
// Deleting on either device removes the record and view on the other.
await phone.sync.sync({sessions:[second],shared:{two:snapshot.shared.two}});
assert.deepEqual([...phone.views.keys()],['two']);assert.ok(phone.removed.includes('one'));
// A genuinely new backend transport may be joined after the previous one died.
phone.views.get('two').connected=false;
await phone.sync.sync({sessions:[second],shared:{two:{connection_id:'replacement'}}});
assert.deepEqual(phone.observed.at(-1),['two','replacement']);
console.log('PASS desktop/phone window synchronization, live-only observation, resume and deletion');
