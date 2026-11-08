/* Touch layout changes presentation only: saved desktop window geometry is retained. */
(function(root){
  'use strict';
  const keys={Escape:'\x1b',Tab:'\t','Ctrl+C':'\x03','Ctrl+D':'\x04','Ctrl+A':'\x01',Up:'A',Down:'B',Right:'C',Left:'D'};
  function keySequence(key,application=false){return ['Up','Down','Right','Left'].includes(key)?(application?'\x1bO':'\x1b[')+keys[key]:keys[key]||'';}
  function bindInput(input, changed) {
    let allowed = false, manual = false, armed = false;
    function sync() {
      input.readOnly = !allowed || (manual && !armed);
      if (manual) input.setAttribute('inputmode', armed ? 'text' : 'none');
      else input.removeAttribute('inputmode');
    }
    function close() {
      if (!armed) return;
      armed = false;
      sync();
      input.blur();
      changed();
    }
    // xterm may focus its textarea internally after a tap. A locked input must
    // stay read-only even when that focus happens outside our event handlers.
    input.addEventListener('focus', () => { if (manual && !armed) input.blur(); });
    input.addEventListener('blur', close);
    return {
      get armed() { return armed; },
      refresh(canControl, manualInput) {
        allowed = canControl;
        manual = manualInput;
        if (!allowed || !manual) close();
        sync();
      },
      toggle() {
        if (armed) { close(); return; }
        if (!manual || !allowed) return;
        armed = true;
        sync();
        input.focus({preventScroll:true});
        changed();
      },
      close
    };
  }
  function install(options){
    const media=matchMedia('(max-width: 740px)');
    const touch=matchMedia('(hover: none) and (pointer: coarse)');
    const menu=document.querySelector('#sidebar-toggle'),scrim=document.querySelector('#mobile-scrim');
    const select=document.querySelector('#mobile-terminal'),sidebar=document.querySelector('#workspace-sidebar');
    const keyboard=document.querySelector('#mobile-keyboard');
    let desktopHidden=localStorage.getItem('sd-sidebar-hidden')==='1';
    const isMobile=()=>media.matches;
    const manualInput=()=>isMobile()||touch.matches;
    function syncSidebar(){
      const open=isMobile()?document.body.classList.contains('mobile-menu-open'):!desktopHidden;
      document.body.classList.toggle('desktop-sidebar-hidden',desktopHidden);
      menu.setAttribute('aria-expanded',String(open));
      menu.setAttribute('aria-label',open?'收起侧栏':'展开侧栏');
      menu.title=open?'收起侧栏':'展开侧栏';
      if(!open&&sidebar.contains(document.activeElement))menu.focus({preventScroll:true});
      sidebar.inert=!open;
    }
    function closeMenu(){document.body.classList.remove('mobile-menu-open');syncSidebar();}
    menu.onclick=()=>{
      if(isMobile())document.body.classList.toggle('mobile-menu-open');
      else{
        desktopHidden=!desktopHidden;
        localStorage.setItem('sd-sidebar-hidden',desktopHidden?'1':'0');
      }
      syncSidebar();
      options.resize();
    };
    scrim.onclick=closeMenu;
    select.onchange=()=>options.select(select.value);
    document.querySelector('#mobile-new').onclick=options.newWindow;
    const keybar=document.querySelector('#mobile-keys');
    keybar.addEventListener('pointerdown',event=>{if(event.target.closest('button'))event.preventDefault();});
    keybar.addEventListener('click',event=>{const button=event.target.closest('button');if(!button)return;if(button.dataset.key)options.key(button.dataset.key);else options.keyboard();});
    let keyboardWasVisible=false;
    function viewport(){
      if(isMobile())document.documentElement.style.setProperty('--app-height',(root.visualViewport?.height||innerHeight)+'px');
      else document.documentElement.style.removeProperty('--app-height');
      const keyboardVisible=manualInput()&&root.visualViewport&&innerHeight-root.visualViewport.height>120;
      if(keyboardWasVisible&&!keyboardVisible)options.closeKeyboard();
      keyboardWasVisible=keyboardVisible;
      document.body.classList.toggle('manual-terminal-input',manualInput());
      document.body.classList.toggle('mobile-keyboard-open',!!keyboardVisible);
      syncSidebar();
      options.resize();
    }
    media.addEventListener('change',()=>{closeMenu();viewport();});
    touch.addEventListener('change',viewport);
    root.visualViewport?.addEventListener('resize',viewport);
    root.addEventListener('resize',viewport);
    let previous='';
    function render(windows,selected){
      const active=windows.find(win=>win.record.id===selected)||windows[0];
      for(const win of windows){
        win.element.classList.toggle('mobile-active',win===active);
        win.inputGate?.refresh(win.canControl,manualInput());
        if(win!==active)win.inputGate?.close();
      }
      const signature=JSON.stringify(windows.map(win=>[win.record.id,win.record.name]));
      if(signature!==previous){
        select.replaceChildren();
        for(const win of windows){const option=document.createElement('option');option.value=win.record.id;option.textContent=win.record.name;select.append(option);}
        previous=signature;
      }
      select.disabled=!active;if(active)select.value=active.record.id;
      keybar.querySelectorAll('button').forEach(button=>{button.disabled=!active||active.canControl===false;});
      const armed=!!active?.inputGate?.armed;
      keyboard.textContent=armed?'收起键盘':'键盘';
      keyboard.setAttribute('aria-pressed',String(armed));
    }
    viewport();
    return {isMobile,manualInput,closeMenu,render};
  }
  root.ScreenDeskMobile={install,keySequence,bindInput};
})(typeof window==='undefined'?globalThis:window);
