/* Terminal colors and local scrolling. No SSH input is generated here. */
(function(root){
  'use strict';
  const palettes={
    light:{background:'#ffffff',foreground:'#1e293b',cursor:'#0f766e',cursorAccent:'#ffffff',selectionBackground:'#b9dcf3',black:'#1e293b',red:'#b42318',green:'#267034',yellow:'#8b6508',blue:'#175cb0',magenta:'#8a3fb0',cyan:'#087f86',white:'#d5dce5',brightBlack:'#64748b',brightRed:'#d92d20',brightGreen:'#31883f',brightYellow:'#9d7600',brightBlue:'#2563eb',brightMagenta:'#a04bc3',brightCyan:'#0c8b95',brightWhite:'#edf2f7'},
    dark:{background:'#000000',foreground:'#d5e1e8',cursor:'#9edbbf',cursorAccent:'#000000',selectionBackground:'#31594f',black:'#172a34',red:'#e58a85',green:'#9dcba6',yellow:'#dfc58b',blue:'#8badcf',magenta:'#b9a0cd',cyan:'#8ac6c4',white:'#dde5e9',brightBlack:'#81939f',brightRed:'#f59b94',brightGreen:'#b1dfb8',brightYellow:'#f1d59a',brightBlue:'#a5c8eb',brightMagenta:'#d2b5e8',brightCyan:'#a0dedd',brightWhite:'#ffffff'}
  };
  function resolveTheme(preference,systemDark){return preference==='light'||preference==='dark'?preference:systemDark?'dark':'light';}
  function palette(theme){return {...palettes[theme==='dark'?'dark':'light']};}
  function installScreenHistory(term){
    const {_bufferService:bufferService,_inputHandler:input}=term._core;
    let inlineBuffer,previousClear;
    const upperRegion=buffer=>buffer.scrollTop===0&&buffer.scrollBottom<term.rows-1;
    function scroll(params,deleting){
      const buffer=bufferService.buffer;
      if(!buffer.hasScrollback||buffer.scrollTop!==0||(deleting&&buffer.y!==0))return false;
      const count=Math.min(params[0]||1,buffer.scrollBottom+1);
      // Screen uses DL for multi-row scrolling inside the upper region.
      // A full-screen DL can be a redraw and must not become history.
      if(deleting&&(!upperRegion(buffer)||count>buffer.scrollBottom))return false;
      if(upperRegion(buffer))inlineBuffer=buffer;
      const erase=input._eraseAttrData();
      for(let i=0;i<count;i++)bufferService.scroll(erase);
      if(deleting)buffer.x=0;
      input.markRangeDirty(0,buffer.scrollBottom);
      return true;
    }
    term.parser.registerCsiHandler({final:'M'},params=>scroll(params,true));
    term.parser.registerCsiHandler({final:'S'},params=>scroll(params,false));
    term.onLineFeed(()=>{
      const buffer=bufferService.buffer;
      if(buffer.hasScrollback&&upperRegion(buffer)&&buffer.y===buffer.scrollBottom)inlineBuffer=buffer;
    });
    term.parser.registerCsiHandler({final:'J'},([mode])=>{
      const buffer=bufferService.buffer;
      const first=previousClear;
      previousClear=null;
      if(mode===3)inlineBuffer=null;
      if(mode!==2||buffer!==inlineBuffer||buffer.y!==0||buffer.x!==0||!buffer.ybase)return false;
      // Screen translates an inline TUI's ED 2 + ED 3 into two full clears.
      // Only an empty second clear of the same frame indicates a rebuild.
      if(first?.buffer===buffer&&first.base===buffer.ybase){
        let blank=true;
        for(let row=0;row<term.rows&&blank;row++){
          blank=!buffer.lines.get(buffer.ybase+row)?.translateToString(true);
        }
        if(blank){
          input.eraseInDisplay({params:[3]});
          inlineBuffer=null;
          return false;
        }
      }
      previousClear={buffer,base:buffer.ybase};
      return false;
    });
  }
  function history(term,archive=''){
    const buffer=term.buffer.normal,lines=[];
    for(let i=Math.max(0,buffer.length-20000);i<buffer.length;i++){
      const line=buffer.getLine(i);
      if(!line)continue;
      const wraps=!!buffer.getLine(i+1)?.isWrapped;
      lines.push(line.translateToString(!wraps)+(wraps?'':'\n'));
    }
    let text=(archive?archive+'\n':'')+lines.join('').replace(/\n+$/,'');
    const rows=text.split('\n');
    if(rows.length>20000)text=rows.slice(-20000).join('\n');
    if(text.length>2097152){
      let start=text.length-2097152;
      // Keep a non-BMP character intact at the bounded snapshot's start.
      if(text.charCodeAt(start)>=0xdc00&&text.charCodeAt(start)<=0xdfff)start++;
      text=text.slice(start);
    }
    return text;
  }
  function installScroll(term,element,options){
    let remainder=0;
    const alternate=()=>term.buffer.active.type==='alternate';
    const stop=event=>{event.preventDefault();event.stopPropagation();};
    const wheel=event=>{
      if(options.historyOpen()||event.ctrlKey||!event.deltaY)return;
      if(options.nativePan?.(event)&&!event.shiftKey){event.stopPropagation();return;}
      // Remote mouse is an explicit per-window opt-in. Shift always allows local review.
      if(options.remoteMouse()&&!event.shiftKey)return;
      stop(event);
      if(alternate()){
        if(event.deltaY<0)options.openHistory();
        return;
      }
      const lineHeight=Math.max(1,options.lineHeight());
      const delta=event.deltaMode===1?event.deltaY:event.deltaMode===2?event.deltaY*term.rows:event.deltaY/lineHeight;
      remainder+=delta;
      const lines=Math.trunc(remainder);
      if(lines){term.scrollLines(lines);remainder-=lines;options.changed();}
    };
    const key=event=>{
      if(event.type!=='keydown'||!event.shiftKey||event.ctrlKey||event.metaKey||event.altKey)return false;
      if(!['PageUp','PageDown','Home','End'].includes(event.key))return false;
      stop(event);
      if(event.key==='End'){options.live();return true;}
      if(alternate()){if(event.key!=='PageDown')options.openHistory();return true;}
      if(event.key==='Home')term.scrollToTop();
      else term.scrollPages(event.key==='PageUp'?-1:1);
      options.changed();return true;
    };
    element.addEventListener('wheel',wheel,{capture:true,passive:false});
    const subscription=term.onScroll(options.changed);
    return {key,dispose(){element.removeEventListener('wheel',wheel,true);subscription.dispose();}};
  }
  root.TerminalView={resolveTheme,palette,installScreenHistory,history,installScroll};
})(typeof window==='undefined'?globalThis:window);
