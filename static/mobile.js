/* Touch layout changes presentation only: saved desktop window geometry is retained. */
(function(root){
  'use strict';
  const keys={Escape:'\x1b',Tab:'\t','Ctrl+C':'\x03','Ctrl+D':'\x04','Ctrl+A':'\x01',Up:'A',Down:'B',Right:'C',Left:'D'};
  function keySequence(key,application=false){return ['Up','Down','Right','Left'].includes(key)?(application?'\x1bO':'\x1b[')+keys[key]:keys[key]||'';}
  function install(options){
    const media=matchMedia('(max-width: 740px)'),menu=document.querySelector('#mobile-menu'),scrim=document.querySelector('#mobile-scrim'),select=document.querySelector('#mobile-terminal'),sidebar=document.querySelector('.sidebar');
    const isMobile=()=>media.matches;
    function closeMenu(){document.body.classList.remove('mobile-menu-open');menu.setAttribute('aria-expanded','false');if(sidebar){if(isMobile()&&sidebar.contains(document.activeElement))menu.focus({preventScroll:true});sidebar.inert=isMobile();}}
    menu.onclick=()=>{const open=document.body.classList.toggle('mobile-menu-open');menu.setAttribute('aria-expanded',String(open));if(sidebar)sidebar.inert=isMobile()&&!open;};
    scrim.onclick=closeMenu;
    select.onchange=()=>options.select(select.value);
    document.querySelector('#mobile-new').onclick=options.newWindow;
    const keybar=document.querySelector('#mobile-keys');
    keybar.addEventListener('pointerdown',event=>{if(event.target.closest('button'))event.preventDefault();});
    keybar.addEventListener('click',event=>{const button=event.target.closest('button');if(!button)return;if(button.dataset.key)options.key(button.dataset.key);else options.keyboard();});
    function viewport(){
      if(isMobile())document.documentElement.style.setProperty('--app-height',(root.visualViewport?.height||innerHeight)+'px');
      else document.documentElement.style.removeProperty('--app-height');
      document.body.classList.toggle('mobile-keyboard-open',isMobile()&&root.visualViewport&&innerHeight-root.visualViewport.height>120);
      if(sidebar)sidebar.inert=isMobile()&&!document.body.classList.contains('mobile-menu-open');
      options.resize();
    }
    media.addEventListener('change',()=>{closeMenu();viewport();});
    root.visualViewport?.addEventListener('resize',viewport);
    root.addEventListener('resize',viewport);
    let previous='';
    function render(windows,selected){
      const active=windows.find(win=>win.record.id===selected)||windows[0];
      for(const win of windows)win.element.classList.toggle('mobile-active',win===active);
      const signature=JSON.stringify(windows.map(win=>[win.record.id,win.record.name]));
      if(signature!==previous){
        select.replaceChildren();
        for(const win of windows){const option=document.createElement('option');option.value=win.record.id;option.textContent=win.record.name;select.append(option);}
        previous=signature;
      }
      select.disabled=!active;if(active)select.value=active.record.id;
      keybar.querySelectorAll('button').forEach(button=>{button.disabled=!active||active.canControl===false;});
    }
    viewport();
    return {isMobile,closeMenu,render};
  }
  root.ScreenDeskMobile={install,keySequence};
})(typeof window==='undefined'?globalThis:window);
