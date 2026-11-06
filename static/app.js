'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const token = new URLSearchParams(location.hash.slice(1)).get('token') || sessionStorage.getItem('sd-token');
if (token) sessionStorage.setItem('sd-token', token);
history.replaceState(null, '', location.pathname);
const windows = new Map();
const systemTheme=window.matchMedia('(prefers-color-scheme: dark)');
let themePreference=localStorage.getItem('sd-theme')||'auto';
if(!['auto','light','dark'].includes(themePreference))themePreference='auto';
function currentTheme(){return TerminalView.resolveTheme(themePreference,systemTheme.matches);}
function applyTheme(){
  const theme=currentTheme();document.documentElement.dataset.theme=theme;
  $('#theme-mode').value=themePreference;
  for(const win of windows.values()){win.term.options.theme=TerminalView.palette(theme);if(!win.element.hidden)win.term.refresh(0,win.term.rows-1);}
}
$('#theme-mode').onchange=event=>{themePreference=event.target.value;localStorage.setItem('sd-theme',themePreference);applyTheme();};
if(systemTheme.addEventListener)systemTheme.addEventListener('change',()=>{if(themePreference==='auto')applyTheme();});
else systemTheme.addListener(()=>{if(themePreference==='auto')applyTheme();});
applyTheme();
const stateNames = {new:'等待连接', connecting:'正在登录', connected:'已连接', detached:'已分离', disconnected:'连接已断开', ended:'已结束'};
let profiles = [], sessions = [], selectedProfile = null, zIndex = 10, selectedWindow = null;
let pendingNew = false, closeTarget = null, shuttingDown = false, activeForwarding = null;
let filePage = null, fileProfile = null, fileForwarding = false, fileBusy = false;
let downloadsTimer = null;
let authTimer=null,authPolling=false,authAnswerBusy=false,activeAuth=null;
let screenScan = null, scanBusy = false, scanSerial = 0;
const profilePolicies = new Map();
const connections = new Map();
function currentSessions(){return sessions.filter(r=>r.profile_id===selectedProfile&&r.state!=='ended');}
function currentWindows(){return [...windows.values()].filter(w=>w.record.profile_id===selectedProfile);}
function absorb(data){
  profiles=data.profiles;const incoming=new Set(data.sessions.map(r=>r.id));
  sessions=sessions.filter(r=>incoming.has(r.id));
  for(const record of data.sessions){const existing=sessions.find(r=>r.id===record.id);if(existing)Object.assign(existing,record);else sessions.push(record);}
  connections.clear();for(const [id,connection] of Object.entries(data.connections||{}))connections.set(id,connection);
}
async function ensureConnection(profile,requested){
  if(requested===undefined&&connections.has(profile.id))return connections.get(profile.id).forwarding;
  const policy=requested===undefined?await chooseForwarding(profile):requested;if(policy===null)return null;
  absorb(await api('/api/connect',{profile_id:profile.id,forwarding:policy}));return policy;
}
async function switchProfile(profile){
  const changed=selectedProfile!==profile.id;
  selectedProfile=profile.id;sessionStorage.setItem('sd-profile',profile.id);
  if(changed){filePage=null;fileProfile=null;$('#remote-path').value='';$('#file-list').replaceChildren(el('p','sidebar-placeholder','点击 → 读取当前层。'));$('#next-file-page').hidden=true;$('#file-page-label').textContent='按需读取';}
  for(const win of windows.values())win.element.hidden=win.record.profile_id!==selectedProfile;
  for(const record of currentSessions())if(record.layout?.visible&&!windows.has(record.id))await openSession(record,false);
  updateCanvas();currentWindows().forEach(fit);renderSidebar();refreshDownloads();
}

function el(tag, className, content) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (content !== undefined) e.textContent = content;
  return e;
}
function toast(text, error = false) {
  const item = el('div', 'toast' + (error ? ' error' : ''), text);
  $('#toast-container').append(item);
  setTimeout(() => item.remove(), error ? 11000 : 4200);
}
async function api(path, data) {
  const options = {headers:{'X-ScreenDesk-Token':token || ''}};
  if (data !== undefined) Object.assign(options, {method:'POST', body:JSON.stringify(data), headers:{...options.headers, 'Content-Type':'application/json'}});
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '操作失败');
  return result;
}
function errorIn(dialog, error) { $('.form-error', dialog).textContent = error.message || error; }
function clearError(dialog) { const target = $('.form-error', dialog); if (target) target.textContent = ''; }
function showDialog(dialog) { clearError(dialog); dialog.showModal(); }
$$('[data-dismiss]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));

function chooseForwarding(profile) {
  return new Promise(resolve => {
    const dialog = $('#forwarding-dialog');
    if (dialog.open) { resolve(null); return; }
    $('#forwarding-description').textContent = '服务器：' + profile.name + '（' + profile.host + '）。请选择本次连接方式。';
    activeForwarding = value => { activeForwarding = null; dialog.close(); if (value !== null) profilePolicies.set(profile.id, value); resolve(value); };
    dialog.addEventListener('close', () => { if (activeForwarding) activeForwarding(null); }, {once:true});
    showDialog(dialog);
  });
}
$('#forwarding-no').onclick = () => activeForwarding?.(false);
$('#forwarding-yes').onclick = () => activeForwarding?.(true);

async function pollSSHAuth(){
  if(shuttingDown||authPolling||authAnswerBusy)return;authPolling=true;
  try{
    const data=await api('/api/auth/prompts');if(authAnswerBusy)return;
    const request=data.prompts[0],dialog=$('#ssh-auth-dialog'),input=$('#ssh-auth-secret');
    if(!request){activeAuth=null;input.value='';if(dialog.open)dialog.close();return;}
    const changed=activeAuth?.id!==request.id;activeAuth=request;
    if(changed){input.value='';clearError(dialog);}
    const confirm=request.kind==='confirm';
    $('#ssh-auth-title').textContent=confirm?'确认 SSH 连接':'SSH 身份认证';
    $('#ssh-auth-context').textContent=request.server+' · '+request.host+' · '+request.purpose;
    $('#ssh-auth-prompt').textContent=request.prompt;
    $('#ssh-auth-details').textContent=request.details||'SSH 尚未返回其他连接信息。';
    if(changed)$('#ssh-auth-details-box').open=confirm;
    $('#ssh-auth-secret-label').hidden=confirm;
    $('#ssh-auth-submit').textContent=confirm?'确认并继续':'提交认证';
    $('#ssh-auth-queue').textContent=data.prompts.length>1?'还有 '+(data.prompts.length-1)+' 个认证请求等待处理':'';
    if(!dialog.open){showDialog(dialog);(confirm?$('#ssh-auth-submit'):input).focus();}
    else if(changed)(confirm?$('#ssh-auth-submit'):input).focus();
  }catch(_){/* Existing app status handles service disconnection; no credentials are logged. */}
  finally{authPolling=false;}
}
async function answerSSHAuth(cancel=false){
  if(!activeAuth||authAnswerBusy)return;
  const request=activeAuth,dialog=$('#ssh-auth-dialog'),input=$('#ssh-auth-secret');
  const answer=request.kind==='confirm'?'yes':input.value;input.value='';authAnswerBusy=true;
  $('#ssh-auth-submit').disabled=true;
  try{await api('/api/auth/'+request.id+'/answer',cancel?{cancel:true}:{answer});if(activeAuth?.id===request.id){activeAuth=null;dialog.close();}}
  catch(error){errorIn(dialog,error);}
  finally{authAnswerBusy=false;$('#ssh-auth-submit').disabled=false;pollSSHAuth();}
}
$('#ssh-auth-form').onsubmit=event=>{event.preventDefault();answerSSHAuth(false);};
$$('[data-auth-cancel]').forEach(button=>button.onclick=()=>answerSSHAuth(true));
$('#ssh-auth-dialog').addEventListener('cancel',event=>{event.preventDefault();answerSSHAuth(true);});

function renderSidebar() {
  const servers = $('#server-list'); servers.replaceChildren();
  if (!profiles.length) servers.append(el('div', 'sidebar-placeholder', 'SSH config 中没有具体 Host 别名，添加后点击 ↻ 刷新。'));
  for (const profile of profiles) {
    const row = el('div', 'server-item' + (profile.id === selectedProfile ? ' selected' : ''));
    const select = el('button', 'server-select');
    select.append(el('span','server-symbol','▤'));
    const text = el('span','server-text'); text.append(el('strong','',profile.name),el('small','',profile.host)); select.append(text);
    if(connections.has(profile.id))text.append(el('small','connection-retained','连接保留'));
    select.onclick = () => switchProfile(profile);
    const edit = el('button', 'server-edit', '⋯'); edit.title='编辑服务器'; edit.onclick = () => openServer(profile);
    row.append(select);if(profile.source!=='ssh_config')row.append(edit);servers.append(row);
  }
  const list = $('#session-list'); list.replaceChildren();
  if (!currentSessions().length) list.append(el('div','sidebar-placeholder','此服务器暂无会话。'));
  for (const record of currentSessions()) {
    const card = el('div','session-card' + (record.id === selectedWindow ? ' active' : ''));
    const title = el('div','session-card-title'); title.append(el('i','state-dot '+record.state),el('strong','',record.name));
    const sub = el('div','session-card-sub'); sub.append(el('span','',record.profile.name),el('span','',stateNames[record.state] || record.state));
    {
      const forget = el('button','','删除本机记录'); forget.title='服务器无响应也可删除；只清理本机，不发送远程结束或分离命令';
      forget.onclick = async event => { event.stopPropagation(); try { await forgetRecord(record.id); } catch(error) { toast(error.message,true); } };
      sub.append(forget);
    }
    card.append(title, sub); card.tabIndex=0; card.setAttribute('role','button');
    card.onclick = () => { if(record.state==='ended') return toast('此 screen 已结束，可新建窗口。'); openSession(record,true); };
    card.onkeydown = event => { if (event.key==='Enter') card.click(); };
    list.append(card);
  }
  const connected = currentSessions().filter(r=>r.state==='connected').length;
  $('#session-count').textContent=currentSessions().length;
  $('#live-count').textContent=connected+' 个连接';
  $('#workspace-summary').textContent=currentWindows().length ? currentWindows().length+' 个窗口 · '+connected+' 个连接' : '尚未打开窗口';
  $('#empty-state').hidden=currentWindows().length>0;
  const profile=profiles.find(p=>p.id===selectedProfile);$('#connection-label').textContent=profile?profile.name+' · '+profile.host:'终端工作台';$('#connection-state').textContent=connections.has(selectedProfile)?'连接保留':'未连接';$('#close-server').disabled=!profile;$('#scan-screens').disabled=!profile;
}

function openServer(profile = null) {
  const form=$('#server-form'); form.reset();
  for(const key of ['id','name','host','user','port','identity']) form.elements[key].value=profile?.[key] || '';
  $('#server-dialog-title').textContent=profile?'编辑服务器':'添加服务器';
  $('#delete-server').hidden=!profile;
  showDialog($('#server-dialog'));
}
$('#add-server').onclick=async()=>{try{const data=await api('/api/profiles/reload',{});absorb(data);if(!profiles.some(p=>p.id===selectedProfile))selectedProfile=profiles[0]?.id||null;for(const win of windows.values())win.element.hidden=win.record.profile_id!==selectedProfile;renderSidebar();toast(data.config_notes?.length?data.config_notes.join('\n'):'SSH config 已重新读取');}catch(error){toast(error.message,true);}};
$('#server-form').onsubmit=async event=>{
  event.preventDefault(); const dialog=$('#server-dialog'), form=event.target;
  const button=$('[type=submit]',form); button.disabled=true;
  try {
    const profile=await api('/api/profiles',Object.fromEntries(new FormData(form)));
    profiles=profiles.filter(p=>p.id!==profile.id).concat(profile); await switchProfile(profile);
    dialog.close(); renderSidebar(); toast('服务器配置已保存');
    if(pendingNew) {pendingNew=false; openNew();}
  } catch(error) {errorIn(dialog,error);} finally {button.disabled=false;}
};
$('#delete-server').onclick=async()=>{
  try {const id=$('#server-form').elements.id.value; await api('/api/profiles/delete',{id}); profiles=profiles.filter(p=>p.id!==id); selectedProfile=profiles[0]?.id || null; $('#server-dialog').close(); filePage=null; renderSidebar();} catch(error){errorIn($('#server-dialog'),error);}
};

// Every deliberate new connection requires an explicit forwarding choice.
const forwardingLabel=el('label','','本窗口是否启用 SSH 配置中的端口转发？');
const forwardingSelect=el('select'); forwardingSelect.name='forwarding'; forwardingSelect.required=true;
for(const [value,text] of [['','请选择本次连接方式'],['0','不启用端口转发'],['1','启用 SSH 配置中的端口转发']]) {const option=el('option','',text);option.value=value;forwardingSelect.append(option);}
forwardingLabel.append(forwardingSelect); $('#new-form .advanced').before(forwardingLabel);
function syncNewConnection(){forwardingLabel.hidden=false;forwardingSelect.required=true;forwardingSelect.value='';}
$('#new-form').elements.profile_id.onchange=syncNewConnection;
function openNew() {
  if(!profiles.length) {toast('请先在 ~/.ssh/config 中添加具体 Host 别名，再点击左侧 ↻。',true);return;}
  const form=$('#new-form'); form.reset(); const select=form.elements.profile_id; select.replaceChildren();
  for(const profile of profiles) {const option=el('option','',profile.name+' · '+profile.host);option.value=profile.id;select.append(option);}
  select.value=selectedProfile || profiles[0].id;syncNewConnection(); showDialog($('#new-dialog'));
}
$('#new-window').onclick=openNew; $('#empty-new').onclick=openNew;
$('#new-form').onsubmit=async event=>{
  event.preventDefault();const button=$('[type=submit]',event.target);button.disabled=true;
  try {
    const data=Object.fromEntries(new FormData(event.target));
    const policy=data.forwarding==='1';data.forwarding=policy;absorb(await api('/api/connect',{profile_id:data.profile_id,forwarding:policy}));
    const record=await api('/api/sessions',data);sessions.push(record);
    profilePolicies.set(record.profile_id,policy);
    $('#new-dialog').close();await switchProfile(record.profile);const win=await openSession(record,false);connectTerminal(win,policy);renderSidebar();
  } catch(error){errorIn($('#new-dialog'),error);} finally{button.disabled=false;}
};

function saveLayout(win) {
  const layout={...win.geometry,fontSize:win.term.options.fontSize,visible:true};
  win.record.layout=layout;
  clearTimeout(win.saveTimer);
  win.saveTimer=setTimeout(()=>api('/api/sessions/'+win.record.id+'/layout',layout).catch(error=>{if(!shuttingDown)toast('布局保存失败：'+error.message,true);}),250);
}
function fit(win) {
  if(!win.element.isConnected||win.element.hidden||win.record.profile_id!==selectedProfile) return;
  try { win.fit.fit(); send(win,{type:'resize',cols:win.term.cols,rows:win.term.rows}); } catch(_){}
}
function applyGeometry(win, geometry, persist=true) {
  win.geometry={x:Math.max(0,Math.round(geometry.x)),y:Math.max(0,Math.round(geometry.y)),width:Math.max(380,Math.round(geometry.width)),height:Math.max(220,Math.round(geometry.height))};
  const g=win.geometry;Object.assign(win.element.style,{left:g.x+'px',top:g.y+'px',width:g.width+'px',height:g.height+'px'});
  updateCanvas();fit(win);if(persist)saveLayout(win);
}
function updateCanvas() {
  let width=$('#viewport').clientWidth,height=$('#viewport').clientHeight;
  for(const win of currentWindows()){width=Math.max(width,win.geometry.x+win.geometry.width+18);height=Math.max(height,win.geometry.y+win.geometry.height+18);}
  Object.assign($('#canvas').style,{width:width+'px',height:height+'px'});
}
function focusWindow(win, scroll=false, focusTerm=true) {
  selectedWindow=win.record.id;windows.forEach(w=>w.element.classList.toggle('focused',w===win));win.element.style.zIndex=++zIndex;
  renderSidebar();if(scroll)win.element.scrollIntoView({block:'nearest',inline:'nearest'});if(focusTerm)win.term.focus();
}
function setState(win, state, ref) {
  win.record.state=state;if(ref){win.record.screen_ref=ref;win.record.ever_ready=true;}
  win.stateLabel.textContent=stateNames[state] || state;win.dot.className='state-dot '+state;
  win.refLabel.textContent=win.record.screen_ref || win.record.remote_name;
  win.reconnect.hidden=state==='connected' || state==='connecting' || state==='ended';renderSidebar();
}
async function openSession(record, shouldConnect) {
  let win=windows.get(record.id);
  if(!win){
    const element=el('article','terminal-window');element.setAttribute('aria-label',record.name+' 终端窗口');
    const header=el('div','window-titlebar');
    header.append(el('span','window-symbol','›_'),el('span','window-name',record.name),el('span','window-server',record.profile.name));
    const actions=el('div','window-actions');
    const forwarding=el('select','window-forwarding');forwarding.title='本窗口端口转发；更改后仅此窗口分离并重新连接';forwarding.setAttribute('aria-label',record.name+' 端口转发');
    for(const [value,text] of [['0','转发:关'],['1','转发:开']]){const option=el('option','',text);option.value=value;forwarding.append(option);}forwarding.value=record.forwarding?'1':'0';
    const mouse=el('button','mouse-button'),diagnose=el('button','diagnose-button','诊断');diagnose.title='只检查当前 screen，不结束任务';
    const fontMinus=el('button','font-button','A−'),fontPlus=el('button','font-button','A＋'),maximize=el('button','','□'),close=el('button','window-close','×');
    fontMinus.title='缩小字号';fontPlus.title='增大字号';maximize.title='最大化 / 还原';close.title='分离或结束 screen';
    actions.append(forwarding,mouse,diagnose,fontMinus,fontPlus,maximize,close);header.append(actions);
    const content=el('div','terminal-content'),footer=el('div','window-footer');
    const dot=el('i','state-dot'),stateLabel=el('span'),reconnect=el('button','window-reconnect','重新连接'),refLabel=el('span','remote-ref');
    const review=el('button','window-review','回看'),live=el('button','window-live','回到底部');review.title='滚轮 / Shift+PageUp 回看；全屏缓冲区读取 screen 历史';live.hidden=true;
    footer.append(dot,stateLabel,reconnect,review,live,refLabel);element.append(header,content,footer);$('#canvas').append(element);
    const term=new Terminal({fontFamily:'Menlo, Monaco, "SFMono-Regular", Consolas, "Liberation Mono", monospace',fontSize:record.layout?.fontSize || 13,lineHeight:1.18,cursorBlink:true,scrollback:20000,convertEol:false,minimumContrastRatio:4.5,theme:TerminalView.palette(currentTheme())});
    const addon=new FitAddon.FitAddon();term.loadAddon(addon);term.open(content);
    const remoteMouse=localStorage.getItem('sd-remote-mouse:'+record.id)==='1';
    const historyView=el('div','terminal-history');historyView.hidden=true;historyView.tabIndex=0;historyView.setAttribute('aria-label',record.name+' 历史回看');
    const historyHeading=el('div','history-heading'),historyTitle=el('span','','screen 历史 · 只读'),historyRefresh=el('button','','刷新'),historyClose=el('button','','返回终端');historyHeading.append(historyTitle,historyRefresh,historyClose);
    const historyScroll=el('div','history-scroll'),historyText=el('pre');historyScroll.append(historyText);historyView.append(historyHeading,historyScroll);content.append(historyView);
    win={record,element,term,fit:addon,dot,stateLabel,reconnect,refLabel,ws:null,geometry:{},maximized:false,forwarding,mouse,remoteMouse,mouseFilter:new TerminalMouseFilter(remoteMouse),decoder:new TextDecoder(),live,historyView,historyText,historyScroll,historyRefresh,historyLoading:false,historySerial:0};windows.set(record.id,win);
    win.scroller=TerminalView.installScroll(term,content,{remoteMouse:()=>win.remoteMouse,historyOpen:()=>!historyView.hidden,lineHeight:()=>content.clientHeight/(term.rows||24),openHistory:()=>openHistory(win),changed:()=>updateScrollState(win),live:()=>returnLive(win)});
    review.onclick=()=>{if(term.buffer.active.type==='normal'&&term.buffer.active.baseY>0){term.scrollPages(-1);updateScrollState(win);}else openHistory(win);};
    live.onclick=()=>returnLive(win);historyClose.onclick=()=>returnLive(win);historyRefresh.onclick=()=>openHistory(win,true);
    historyView.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();returnLive(win);}});
    function updateMouse(){mouse.textContent=win.remoteMouse?'鼠标:远程':'鼠标:本地';mouse.title=win.remoteMouse?'鼠标坐标会发送到远程，点击关闭上报':'已拦截鼠标坐标上报；点击允许远程使用鼠标';}
    updateMouse();mouse.onclick=()=>{win.remoteMouse=!win.remoteMouse;localStorage.setItem('sd-remote-mouse:'+record.id,win.remoteMouse?'1':'0');term.write(win.mouseFilter.setRemote(win.remoteMouse));updateMouse();};
    forwarding.onchange=()=>changeForwarding(win,forwarding.value==='1');
    diagnose.onclick=async()=>{showDialog($('#diagnostic-dialog'));$('#diagnostic-output').textContent='正在检查当前会话…';try{const result=await api('/api/sessions/'+record.id+'/diagnose',{});$('#diagnostic-output').textContent=result.output||'服务器没有返回诊断输出';}catch(error){$('#diagnostic-output').textContent=error.message;}};
    const offset=(windows.size-1)%7*28;
    applyGeometry(win,{x:record.layout?.x??(22+offset),y:record.layout?.y??(20+offset),width:record.layout?.width??Math.max(360,Math.min(760,$('#viewport').clientWidth-48)),height:record.layout?.height??Math.max(250,Math.min(470,$('#viewport').clientHeight-48))},false);
    term.onData(data=>{const filtered=win.mouseFilter.input(data);if(filtered)send(win,{type:'input',data:filtered});});
    element.addEventListener('pointerdown',event=>focusWindow(win,false,!event.target.closest('button,select,.terminal-history')));
    header.addEventListener('pointerdown',event=>{if(!event.target.closest('button,select'))startDrag(event,win,'move');});
    header.ondblclick=event=>{if(!event.target.closest('button,select'))toggleMaximize(win);};
    for(const direction of ['n','s','e','w','ne','nw','se','sw']){const handle=el('div','resize-handle resize-'+direction);handle.onpointerdown=event=>startDrag(event,win,direction);element.append(handle);}
    fontMinus.onclick=()=>changeFont(win,-1);fontPlus.onclick=()=>changeFont(win,1);maximize.onclick=()=>toggleMaximize(win);close.onclick=()=>openClose(win);
    reconnect.onclick=()=>reconnectWindow(win);
    term.attachCustomKeyEventHandler(event=>{
      if(win.scroller.key(event))return false;
      if(event.type==='keydown' && event.ctrlKey && event.shiftKey && event.code==='KeyC') {navigator.clipboard?.writeText(term.getSelection()).catch(()=>{});return false;}
      return true;
    });
    setState(win,record.state==='connected'?'disconnected':record.state);
    term.writeln('\x1b[38;5;109mScreenDesk · '+record.name.replace(/[\x00-\x1f\x7f]/g,'')+'\x1b[0m');
    term.writeln('\x1b[38;5;242m点击“重新连接”恢复远程 screen。\x1b[0m\r\n');
    saveLayout(win);
  }
  focusWindow(win,true);
  if(shouldConnect && (!win.ws || win.ws.readyState>1)) await reconnectWindow(win);
  return win;
}
function send(win,data) {if(win.ws?.readyState===WebSocket.OPEN)win.ws.send(JSON.stringify(data));}
function updateScrollState(win){win.live.hidden=win.historyView.hidden&&win.term.buffer.active.viewportY>=win.term.buffer.active.baseY;}
function returnLive(win){win.historySerial++;win.historyView.hidden=true;win.term.scrollToBottom();updateScrollState(win);win.term.focus();}
async function openHistory(win,refresh=false){
  if(win.historyLoading)return;
  if(!refresh&&!win.historyView.hidden)return;
  win.historyView.hidden=false;win.historyView.focus();updateScrollState(win);
  if(!win.record.ever_ready){win.historyText.textContent='这个窗口尚未建立远程 screen。连接后可回看它保存的历史。';return;}
  const serial=++win.historySerial;win.historyLoading=true;win.historyRefresh.disabled=true;win.historyText.textContent='正在读取 screen 保留的历史…';
  try{
    const data=await api('/api/sessions/'+win.record.id+'/history',{});
    if(serial!==win.historySerial||!win.element.isConnected)return;
    win.historyText.textContent=data.text||'screen 尚未保存可回看的内容。';
    requestAnimationFrame(()=>{if(serial===win.historySerial)win.historyScroll.scrollTop=Math.max(0,win.historyScroll.scrollHeight-win.historyScroll.clientHeight*1.7);});
  }catch(error){if(serial===win.historySerial)win.historyText.textContent='读取历史未完成：'+error.message+'\n可点刷新重试；终端连接与远程任务保持原状态。';}
  finally{win.historyLoading=false;win.historyRefresh.disabled=false;}
}
async function reconnectWindow(win) {
  if(win.ws && win.ws.readyState<2) return;
  try{const policy=await ensureConnection(win.record.profile,win.forwarding.value==='1');if(policy===null)return;connectTerminal(win,policy);}catch(error){toast(error.message,true);}
}
async function changeForwarding(win,policy){
  const previous=!!win.record.forwarding,wasConnected=win.ws&&win.ws.readyState<2;win.forwarding.disabled=true;
  try{
    if(wasConnected){const record=await api('/api/sessions/'+win.record.id+'/close',{action:'detach'});Object.assign(win.record,record);win.ws?.close();setState(win,'detached');}
    await api('/api/sessions/'+win.record.id+'/options',{forwarding:policy});win.record.forwarding=policy;saveLayout(win);
    if(wasConnected){await ensureConnection(win.record.profile,policy);connectTerminal(win,policy);}
  }catch(error){win.forwarding.value=previous?'1':'0';toast(error.message,true);}finally{win.forwarding.disabled=false;}
}
function connectTerminal(win,forwarding) {
  if(win.ws && win.ws.readyState<2)return;
  returnLive(win);
  win.mouseFilter=new TerminalMouseFilter(win.remoteMouse);win.decoder=new TextDecoder();win.term.reset();win.term.writeln('\x1b[38;5;109m正在连接 '+win.record.profile.host+' …\x1b[0m\r\n');win.record.forwarding=forwarding;win.forwarding.value=forwarding?'1':'0';
  setState(win,'connecting');
  const socket=new WebSocket('ws://'+location.host+'/ws/'+win.record.id+'?cols='+win.term.cols+'&rows='+win.term.rows+'&forwarding='+(forwarding?'1':'0'),['screen-desk',token]);
  win.ws=socket;socket.binaryType='arraybuffer';let hadOpen=false,heartbeat=null;
  socket.onopen=()=>{if(win.ws!==socket)return;hadOpen=true;fit(win);win.term.focus();heartbeat=setInterval(()=>{if(win.ws===socket)send(win,{type:'ping'});},15000);win.heartbeat=heartbeat;};
  socket.onmessage=event=>{
    if(win.ws!==socket)return;
    if(event.data instanceof ArrayBuffer){const bytes=event.data.byteLength,text=win.mouseFilter.feed(win.decoder.decode(event.data,{stream:true}));win.term.write(text,()=>{if(win.ws===socket&&socket.readyState===WebSocket.OPEN)socket.send(JSON.stringify({type:'ack',bytes}));});return;}
    const data=JSON.parse(event.data);
    if(data.type==='state')setState(win,data.state,data.screen_ref);
    if(data.type==='error')win.term.writeln('\r\n\x1b[31m'+data.message.replace(/[\x00-\x1f\x7f]/g,' ')+'\x1b[0m');
    if(data.type==='exit') {setState(win,data.state);win.term.writeln('\r\n\x1b[38;5;245m连接已断开。远程 screen 不会因掉线被结束；可点击“重新连接”。\x1b[0m');socket.close();}
  };
  socket.onclose=()=>{clearInterval(heartbeat);if(win.ws!==socket)return;if(windows.has(win.record.id) && !['ended','detached'].includes(win.record.state))setState(win,'disconnected');if(!hadOpen && !shuttingDown)toast('连接未建立。请确认程序仍在运行，并且该窗口未在其他标签中打开。',true);};
  socket.onerror=()=>{};
}
function changeFont(win,delta){win.term.options.fontSize=Math.max(10,Math.min(28,win.term.options.fontSize+delta));fit(win);saveLayout(win);}
function toggleMaximize(win){
  if(!win.maximized){win.restoreGeometry={...win.geometry};win.maximized=true;applyGeometry(win,{x:$('#viewport').scrollLeft+10,y:$('#viewport').scrollTop+10,width:Math.max(320,$('#viewport').clientWidth-24),height:Math.max(220,$('#viewport').clientHeight-24)},false);}
  else{win.maximized=false;applyGeometry(win,win.restoreGeometry);}
  win.element.classList.toggle('maximized',win.maximized);focusWindow(win);
}
function startDrag(event,win,direction){
  if(event.button!==0 || win.maximized)return;event.preventDefault();focusWindow(win);
  const target=event.currentTarget,startX=event.clientX,startY=event.clientY,start={...win.geometry};
  target.setPointerCapture(event.pointerId);document.body.classList.add('dragging');
  const move=e=>{const dx=e.clientX-startX,dy=e.clientY-startY;let g={...start};
    if(direction==='move'){g.x=Math.max(0,start.x+dx);g.y=Math.max(0,start.y+dy);}
    else {if(direction.includes('e'))g.width=Math.max(380,start.width+dx);if(direction.includes('s'))g.height=Math.max(220,start.height+dy);if(direction.includes('w')){g.x=Math.max(0,Math.min(start.x+dx,start.x+start.width-380));g.width=start.width+start.x-g.x;}if(direction.includes('n')){g.y=Math.max(0,Math.min(start.y+dy,start.y+start.height-220));g.height=start.height+start.y-g.y;}}
    applyGeometry(win,g,false);
  };
  const end=()=>{target.removeEventListener('pointermove',move);target.removeEventListener('pointerup',end);target.removeEventListener('pointercancel',end);document.body.classList.remove('dragging');saveLayout(win);};
  target.addEventListener('pointermove',move);target.addEventListener('pointerup',end);target.addEventListener('pointercancel',end);
}
function destroyWindow(id){const win=windows.get(id);if(!win)return;clearTimeout(win.saveTimer);clearInterval(win.heartbeat);win.historySerial++;win.scroller.dispose();win.ws?.close();win.term.dispose();win.element.remove();windows.delete(id);if(selectedWindow===id)selectedWindow=null;updateCanvas();renderSidebar();}
async function forgetRecord(id){const win=windows.get(id);if(win)clearTimeout(win.saveTimer);await api('/api/sessions/'+id+'/forget',{});sessions=sessions.filter(r=>r.id!==id);destroyWindow(id);renderSidebar();}
function renderScreenScan(){
  const list=$('#screen-scan-results');list.replaceChildren();if(!screenScan)return;
  const entries=screenScan.entries,showIgnored=$('#show-ignored-screens').checked;
  const visible=entries.filter(entry=>showIgnored||!entry.ignored);
  $('#screen-scan-summary').textContent='共 '+entries.length+' 个 screen · '+entries.filter(e=>e.added).length+' 个已在本机 · '+entries.filter(e=>e.ignored).length+' 个已忽略';
  if(!visible.length)list.append(el('p','dialog-description',entries.length?'其余会话已忽略，勾选“显示已忽略”可恢复。':'这个 SSH 账号当前没有 screen。'));
  for(const entry of visible){
    const row=el('div','session-check-row screen-scan-row'),heading=el('div','download-heading');
    const name=el('strong','',entry.name),status=el('span','',{attached:'已附加',detached:'已分离',dead:'失效 socket',unknown:'状态未知'}[entry.state]||entry.status);
    heading.append(name,status);const ref=el('div','screen-scan-ref',entry.screen_ref);
    const message=el('div','download-message',entry.reason||(entry.managed?'按命名规则识别，名称与另一台电脑保持一致。':'普通 screen，可手动添加到本机。'));
    const actions=el('div','download-actions'),add=el('button','secondary-button',entry.added?'已在本机':'添加到本机'),ignore=el('button','secondary-button',entry.ignored?'取消忽略':'忽略');
    add.disabled=entry.added||!entry.addable||entry.ignored;
    const context={profile_id:screenScan.profile.id,scan_id:screenScan.scan_id,screen_ref:entry.screen_ref};
    add.onclick=async()=>{add.disabled=true;try{const record=await api('/api/screens/add',context);sessions.push(record);entry.added=true;if(selectedProfile===record.profile_id)await openSession(record,false);renderSidebar();renderScreenScan();toast('已添加“'+record.name+'”；点击重新连接才会连接服务器。');}catch(error){toast(error.message,true);add.disabled=entry.added||!entry.addable||entry.ignored;}};
    ignore.onclick=async()=>{ignore.disabled=true;try{await api('/api/screens/ignore',{...context,ignored:!entry.ignored});entry.ignored=!entry.ignored;renderScreenScan();}catch(error){toast(error.message,true);ignore.disabled=false;}};
    actions.append(add,ignore);row.append(heading,ref,message,actions);list.append(row);
  }
}
async function runScreenScan(profile,forwarding){
  const serial=++scanSerial,dialog=$('#screen-scan-dialog');scanBusy=true;screenScan=null;
  $('#screen-scan-results').replaceChildren();$('#screen-scan-summary').textContent='正在扫描…';$('#screen-scan-title').textContent='服务器 screen · '+profile.name;
  $('#refresh-screen-scan').disabled=true;clearError(dialog);if(!dialog.open)showDialog(dialog);
  try{const data=await api('/api/screens/scan',{profile_id:profile.id,forwarding});if(serial!==scanSerial)return;screenScan={...data,profile,forwarding};renderScreenScan();if(data.notes?.length)errorIn(dialog,data.notes.join('\n'));}
  catch(error){if(serial===scanSerial){$('#screen-scan-summary').textContent='扫描未完成；本机记录保持不变。';errorIn(dialog,error);}}
  finally{if(serial===scanSerial){scanBusy=false;$('#refresh-screen-scan').disabled=false;}}
}
$('#scan-screens').onclick=async()=>{const profile=profiles.find(p=>p.id===selectedProfile);if(!profile||scanBusy)return;try{const policy=await ensureConnection(profile);if(policy!==null)await runScreenScan(profile,policy);}catch(error){toast(error.message,true);}};
$('#refresh-screen-scan').onclick=async()=>{if(scanBusy)return;const profile=screenScan?.profile||profiles.find(p=>p.id===selectedProfile);if(!profile)return;try{const policy=screenScan?.forwarding??await ensureConnection(profile);if(policy!==null)await runScreenScan(profile,policy);}catch(error){toast(error.message,true);}};
$('#show-ignored-screens').onchange=renderScreenScan;
$('#verify-sessions').onclick=async()=>{
  const records=currentSessions().slice(),list=$('#session-check-results');list.replaceChildren();showDialog($('#session-check-dialog'));
  if(!records.length){list.append(el('p','dialog-description','当前服务器没有本机会话记录。'));return;}
  const tasks=records.map(record=>{
    const row=el('div','session-check-row'),heading=el('div','download-heading'),name=el('strong','',record.name),status=el('span','','等待核对');heading.append(name,status);
    const message=el('div','download-message',''),actions=el('div','download-actions'),check=el('button','secondary-button','重新核对'),forget=el('button','secondary-button','移除本机记录');check.disabled=true;
    const run=async()=>{
      check.disabled=true;status.textContent='核对中…';
      try{
        const result=await api('/api/sessions/'+record.id+'/check',{});message.textContent=result.message;
        status.textContent={exists:'远程存在',missing:'远程已关闭',unknown:'无法确认 · 保留',not_created:'尚未创建'}[result.status]||result.status;
        if(result.status==='missing'){sessions=sessions.filter(r=>r.id!==record.id);destroyWindow(record.id);renderSidebar();}
      }catch(error){status.textContent='未完成 · 保留';message.textContent=error.message;}finally{check.disabled=false;}
    };
    check.onclick=run;forget.onclick=async()=>{forget.disabled=true;try{await forgetRecord(record.id);status.textContent='本机记录已移除';message.textContent='未结束远程 screen。';actions.remove();}catch(error){message.textContent=error.message;forget.disabled=false;}};
    actions.append(check,forget);row.append(heading,message,actions);list.append(row);return run;
  });
  // Two independent targeted probes at a time; never scan every remote socket.
  let index=0;const worker=async()=>{while(index<tasks.length){const task=tasks[index++];await task();}};
  await Promise.all([worker(),worker()]);
};
function openClose(win){closeTarget=win;$('#close-title').textContent='关闭“'+win.record.name+'”';$('#close-description').textContent='请选择服务器上这个 screen 的处理方式。';$('#terminate-button').disabled=!win.record.ever_ready;showDialog($('#close-dialog'));}
async function closeWindow(action){
  const win=closeTarget;if(!win)return;const dialog=$('#close-dialog');$$('.close-choice:not(.local-record-choice)',dialog).forEach(b=>b.disabled=true);clearError(dialog);clearTimeout(win.saveTimer);
  try{const record=await api('/api/sessions/'+win.record.id+'/close',{action});Object.assign(win.record,record);destroyWindow(record.id);dialog.close();toast(action==='detach'?'已分离；远程任务继续运行。':'screen 已结束。');}
  catch(error){errorIn(dialog,error);}finally{$$('#close-dialog .close-choice:not(.local-record-choice)').forEach(b=>b.disabled=false);if(closeTarget===win)$('#terminate-button').disabled=!win.record.ever_ready;}
}
$('#detach-button').onclick=()=>closeWindow('detach');$('#terminate-button').onclick=()=>closeWindow('terminate');
const localDelete=el('button','close-choice local-record-choice');localDelete.id='forget-button';
const localDeleteText=el('span');localDeleteText.append(el('strong','','仅删除本机记录'),el('small','','服务器无响应、screen 卡住或创建失败时也可删除。远程 screen 和任务保持不变。'));
localDelete.append(el('span','choice-icon','⌫'),localDeleteText,el('span','choice-arrow','→'));$('#terminate-button').after(localDelete);
localDelete.onclick=async()=>{const win=closeTarget;if(!win)return;localDelete.disabled=true;try{await forgetRecord(win.record.id);if(closeTarget===win)$('#close-dialog').close();toast('本机记录已删除；未发送远程 screen 控制命令。');}catch(error){errorIn($('#close-dialog'),error);}finally{localDelete.disabled=false;}};
function renderServerChoices(){
  const list=$('#server-session-choices');list.replaceChildren();
  for(const record of currentSessions()){
    const row=el('div','server-close-row');const name=el('span','',record.name);name.title=record.screen_ref||record.remote_name;
    const select=el('select');select.dataset.sessionId=record.id;
    const detach=el('option','','Detach · 保留任务');detach.value='detach';
    const close=el('option','','Close · 结束并移除');close.value='terminate';close.disabled=!record.ever_ready;
    select.append(detach,close);row.append(name,select);list.append(row);
  }
  if(!currentSessions().length)list.append(el('p','dialog-description','这台服务器没有终端会话，将断开本机连接并暂停下载。'));
}
$('#close-server').onclick=()=>{const profile=profiles.find(p=>p.id===selectedProfile);if(!profile)return;$('#close-server-title').textContent='关闭服务器 · '+profile.name;renderServerChoices();showDialog($('#close-server-dialog'));};
$('#confirm-close-server').onclick=async()=>{
  const button=$('#confirm-close-server'),dialog=$('#close-server-dialog');button.disabled=true;clearError(dialog);
  const profile=profiles.find(p=>p.id===selectedProfile);
  try{
    const decisions=Object.fromEntries($$('#server-session-choices select').map(select=>[select.dataset.sessionId,select.value]));
    await Promise.all(currentWindows().map(win=>{clearTimeout(win.saveTimer);return api('/api/sessions/'+win.record.id+'/layout',{...win.geometry,fontSize:win.term.options.fontSize,visible:true});}));
    const data=await api('/api/close-server',{profile_id:profile.id,decisions});
    const retained=new Set(data.sessions.map(r=>r.id));
    for(const id of [...windows.keys()])if(!retained.has(id))destroyWindow(id);
    absorb(data);
    for(const win of currentWindows()){win.ws?.close();setState(win,win.record.state,win.record.screen_ref);}
    await switchProfile(profile);dialog.close();toast('服务器已关闭。Detached 窗口保留布局，点“重新连接”才连接。');
  }catch(error){
    const message=error.message;try{const data=await api('/api/workspace');const retained=new Set(data.sessions.map(r=>r.id));for(const id of [...windows.keys()])if(!retained.has(id))destroyWindow(id);absorb(data);renderServerChoices();renderSidebar();}catch(_){}
    errorIn(dialog,message);
  }finally{button.disabled=false;}
};
$('#tile-windows').onclick=()=>{
  const all=currentWindows();if(!all.length)return;const columns=Math.max(1,Math.min(all.length,Math.floor($('#viewport').clientWidth/400))),rows=Math.ceil(all.length/columns),width=Math.max(320,Math.floor(($('#viewport').clientWidth-32)/columns)-12),height=Math.max(220,Math.floor(($('#viewport').clientHeight-32)/rows)-12);
  all.forEach((win,i)=>{win.maximized=false;win.element.classList.remove('maximized');applyGeometry(win,{x:16+(i%columns)*(width+12),y:16+Math.floor(i/columns)*(height+12),width,height});});$('#viewport').scrollTo(0,0);
};
$('#cascade-windows').onclick=()=>{currentWindows().forEach((win,i)=>{win.maximized=false;win.element.classList.remove('maximized');applyGeometry(win,{x:22+i%10*28,y:20+i%10*28,width:Math.max(360,Math.min(760,$('#viewport').clientWidth-64)),height:Math.max(250,Math.min(470,$('#viewport').clientHeight-64))});});};
window.addEventListener('resize',()=>{updateCanvas();windows.forEach(fit);});
window.addEventListener('keydown',event=>{if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k'){event.preventDefault();if(!$('dialog[open]'))openNew();}});
$('#workspace-nav').onclick=()=>$('#viewport').scrollTo(0,0);$('.brand').onclick=event=>{event.preventDefault();$('#viewport').scrollTo(0,0);};
$('#help-button').onclick=()=>showDialog($('#help-dialog'));
$('#exit-app').onclick=()=>showDialog($('#quit-dialog'));
$('#confirm-quit').onclick=async()=>{
  try{shuttingDown=true;clearInterval(downloadsTimer);clearInterval(authTimer);$('#ssh-auth-secret').value='';await api('/api/shutdown',{});$('#quit-dialog').close();const message=el('div','shutdown-message');message.append(el('h1','','ScreenDesk 已退出'),el('p','','远程 screen 与任务继续运行。\n下次双击启动器即可恢复工作区。'));document.body.append(message);}catch(error){shuttingDown=false;errorIn($('#quit-dialog'),error);authTimer=setInterval(pollSSHAuth,500);}
};

function humanSize(size){if(size==null)return '未知大小';if(size<1024)return size+' B';const power=Math.min(4,Math.floor(Math.log(size)/Math.log(1024)));return (size/1024**power).toFixed(1)+' '+['B','KB','MB','GB','TB'][power];}
function remoteJoin(path,name){return (path==='/'?'':path)+'/'+name;}
function setFilesOpen(open){
  $('#file-drawer').hidden=!open;$('#toggle-files').setAttribute('aria-expanded',String(open));$('#toggle-files').replaceChildren(el('span','',open?'▾ 文件列表':'▸ 文件列表'),el('span','',open?'折叠':'展开'));localStorage.setItem('sd-files-open',open?'1':'0');updateCanvas();currentWindows().forEach(fit);
}
$('#toggle-files').onclick=()=>setFilesOpen($('#file-drawer').hidden);$('#collapse-files').onclick=()=>setFilesOpen(false);
const fileWidth=Number(localStorage.getItem('sd-files-width'))||370;$('#file-drawer').style.width=Math.max(260,Math.min(760,fileWidth))+'px';
$('#file-drawer-resize').onpointerdown=event=>{
  event.preventDefault();const handle=event.currentTarget,start=event.clientX,width=$('#file-drawer').getBoundingClientRect().width;handle.setPointerCapture(event.pointerId);
  const move=e=>{$('#file-drawer').style.width=Math.max(260,Math.min(760,width+e.clientX-start))+'px';};
  const end=()=>{handle.removeEventListener('pointermove',move);handle.removeEventListener('pointerup',end);handle.removeEventListener('pointercancel',end);localStorage.setItem('sd-files-width',String($('#file-drawer').getBoundingClientRect().width));updateCanvas();currentWindows().forEach(fit);};
  handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',end);handle.addEventListener('pointercancel',end);
};
async function readFiles(next=false,pathOverride=null){
  if(fileBusy)return;const profile=profiles.find(p=>p.id===selectedProfile);if(!profile){toast('请先选择 SSH config 中的服务器。',true);return;}
  fileBusy=true;$('#file-page-label').textContent='读取中…';
  try{
    const policy=await ensureConnection(profile);if(policy===null)return;
    const result=await api('/api/files',{profile_id:profile.id,forwarding:policy,path:next?filePage.path:(pathOverride??($('#remote-path').value.trim()||'.')),cursor:next?filePage?.cursor:null});
    if(selectedProfile!==profile.id)return;
    filePage=result;fileProfile=profile;fileForwarding=policy;$('#remote-path').value=result.path;renderFiles();
  }catch(error){toast(error.message,true);$('#file-page-label').textContent='读取失败 · 可刷新';}finally{fileBusy=false;if(!filePage)$('#file-page-label').textContent='按需读取';}
}
function renderFiles(){
  if(!filePage)return;const list=$('#file-list');list.replaceChildren();$('#file-page-label').textContent='第 '+filePage.page+' 页 · '+filePage.entries.length+' 项';
  $('#next-file-page').hidden=!filePage.more;
  const entries=[...filePage.entries].sort((a,b)=>(a.kind==='directory'?-1:0)-(b.kind==='directory'?-1:0)||a.name.localeCompare(b.name));
  function row(item){const e=el('div','file-row');const name=el('button','file-name');name.title=item.name;name.append(el('span','file-icon',item.kind==='directory'?'▸':item.kind==='link'?'↗':'·'),el('span','file-basename',item.name));
    name.onclick=()=>{if(item.unsupported)return toast('此文件名不是 UTF-8，无法安全操作。',true);if(item.kind==='directory'||item.kind==='link')readFiles(false,remoteJoin(filePage.path,item.name));};
    e.append(name);
    if(item.kind!=='directory'&&!item.unsupported){const down=el('button','file-download','↓');down.title='下载 '+item.name+'（'+humanSize(item.size)+'）';down.onclick=async()=>{try{await api('/api/downloads',{profile_id:fileProfile.id,forwarding:fileForwarding,path:remoteJoin(filePage.path,item.name)});toast('已加入下载任务');refreshDownloads();}catch(error){toast(error.message,true);}};e.append(down);}
    return e;
  }
  if(!entries.length)list.append(el('p','sidebar-placeholder','当前目录为空。'));
  if(!$('#group-extensions').checked){for(const item of entries)list.append(row(item));return;}
  const groups=new Map();for(const item of entries){let key;if(item.kind==='directory')key='文件夹';else {const match=item.name.match(/(\.[^.]+)$/);key=match?match[1].toLowerCase():'无后缀';}if(!groups.has(key))groups.set(key,[]);groups.get(key).push(item);}
  for(const [suffix,items] of groups){const details=el('details','extension-group');details.open=items.length<=15||suffix==='文件夹';const summary=el('summary','',suffix+'  ');summary.append(el('span','',String(items.length)));details.append(summary);for(const item of items)details.append(row(item));list.append(details);}
}
$('#path-form').onsubmit=event=>{event.preventDefault();readFiles();};$('#open-files').onclick=()=>readFiles();$('#refresh-files').onclick=()=>readFiles();$('#next-file-page').onclick=()=>readFiles(true);$('#group-extensions').onchange=renderFiles;
$('#parent-directory').onclick=()=>{const path=filePage?.path||$('#remote-path').value||'.';readFiles(false,path==='/'?'/':path.slice(0,path.lastIndexOf('/'))||'/');};

const downloadStates={queued:'等待连接',running:'下载中',retrying:'掉线重试中',paused:'已暂停',pausing:'正在暂停',changed:'文件变化 / 需处理',completed:'已完成',cancelled:'已取消'};
async function refreshDownloads(){
  if(shuttingDown)return;
  try{const data=await api('/api/downloads');$('#download-folder').textContent=data.folder;
    data.jobs=data.jobs.filter(j=>j.profile.id===selectedProfile);
    $('#download-count').textContent=data.jobs.filter(j=>!['completed','cancelled'].includes(j.state)).length;
    if(!$('#downloads-dialog').open)return;
    const list=$('#download-list');list.replaceChildren();if(!data.jobs.length)list.append(el('p','dialog-description','在左侧文件列表点击 ↓ 开始下载。'));
    for(const job of data.jobs.slice().reverse()){
      const item=el('div','download-item'),heading=el('div','download-heading');heading.append(el('strong','',job.name),el('span','',downloadStates[job.state]||job.state));item.append(heading);
      const progress=el('progress');progress.max=job.total||1;progress.value=job.downloaded;item.append(progress);
      item.append(el('div','download-detail',humanSize(job.downloaded)+' / '+humanSize(job.total)+' · '+job.profile.name));
      const message=el('div','download-message',job.message);message.title=job.message;item.append(message);
      const actions=el('div','download-actions');
      const addAction=(text,action)=>{const button=el('button','secondary-button',text);button.onclick=async()=>{button.disabled=true;try{let body={};if(['resume','restart'].includes(action)){const policy=await ensureConnection(job.profile);if(policy===null)return;body.forwarding=policy;}await api('/api/downloads/'+job.id+'/'+action,body);refreshDownloads();}catch(error){toast(error.message,true);}finally{button.disabled=false;}};actions.append(button);};
      if(['queued','running','retrying'].includes(job.state))addAction('暂停','pause');
      if(['paused','cancelled'].includes(job.state))addAction('继续','resume');
      if(job.state==='changed')addAction('保留旧文件并重新下载','restart');
      if(!['completed','cancelled','pausing'].includes(job.state))addAction('取消','cancel');
      addAction('在本机显示','reveal');item.append(actions);list.append(item);
    }
  }catch(_){if(!shuttingDown)$('#app-status').textContent='本机服务未连接；远程 screen 保留';}
}
$('#downloads-button').onclick=()=>{showDialog($('#downloads-dialog'));refreshDownloads();};

async function boot(){
  try{if(!token)throw new Error('请双击启动器打开 ScreenDesk。');const data=await api('/api/workspace');$('#build-version').textContent=data.build?' · '+data.build:'';absorb(data);selectedProfile=profiles.find(p=>p.id===sessionStorage.getItem('sd-profile'))?.id||data.active_profile?.id||profiles[0]?.id||null;
    // Restore layout without making a connection before the forwarding question.
    for(const record of currentSessions())if(record.layout?.visible)await openSession(record,false);
    setFilesOpen(localStorage.getItem('sd-files-open')==='1');renderSidebar();refreshDownloads();downloadsTimer=setInterval(refreshDownloads,1800);pollSSHAuth();authTimer=setInterval(pollSSHAuth,500);
  }catch(error){$('#app-status').textContent=error.message;$('#app-status').classList.add('fatal');toast(error.message,true);}
}
boot();
