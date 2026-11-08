/* Terminal colors, local history and wheel routing. */
(function(root){
  'use strict';
  const palettes={
    light:{background:'#ffffff',foreground:'#1e293b',cursor:'#0f766e',cursorAccent:'#ffffff',selectionBackground:'#b9dcf3',black:'#1e293b',red:'#b42318',green:'#267034',yellow:'#8b6508',blue:'#175cb0',magenta:'#8a3fb0',cyan:'#087f86',white:'#d5dce5',brightBlack:'#64748b',brightRed:'#d92d20',brightGreen:'#31883f',brightYellow:'#9d7600',brightBlue:'#2563eb',brightMagenta:'#a04bc3',brightCyan:'#0c8b95',brightWhite:'#edf2f7'},
    dark:{background:'#000000',foreground:'#d5e1e8',cursor:'#9edbbf',cursorAccent:'#000000',selectionBackground:'#31594f',black:'#172a34',red:'#e58a85',green:'#9dcba6',yellow:'#dfc58b',blue:'#8badcf',magenta:'#b9a0cd',cyan:'#8ac6c4',white:'#dde5e9',brightBlack:'#81939f',brightRed:'#f59b94',brightGreen:'#b1dfb8',brightYellow:'#f1d59a',brightBlue:'#a5c8eb',brightMagenta:'#d2b5e8',brightCyan:'#a0dedd',brightWhite:'#ffffff'}
  };
  function resolveTheme(preference,systemDark){return preference==='light'||preference==='dark'?preference:systemDark?'dark':'light';}
  function palette(theme){return {...palettes[theme==='dark'?'dark':'light']};}
  function installScreenHistory(term,archiveUntil){
    const {_bufferService:bufferService,_inputHandler:input}=term._core;
    const upperRegion=buffer=>buffer.scrollTop===0&&buffer.scrollBottom<term.rows-1;
    function scroll(params,deleting){
      const buffer=bufferService.buffer;
      if(!buffer.hasScrollback||buffer.scrollTop!==0||(deleting&&buffer.y!==0))return false;
      const count=Math.min(params[0]||1,buffer.scrollBottom+1);
      // Screen uses DL for multi-row scrolling inside the upper region.
      // A full-screen DL can be a redraw and must not become history.
      if(deleting&&(!upperRegion(buffer)||count>buffer.scrollBottom))return false;
      const erase=input._eraseAttrData();
      for(let i=0;i<count;i++)bufferService.scroll(erase);
      if(deleting)buffer.x=0;
      input.markRangeDirty(0,buffer.scrollBottom);
      return true;
    }
    term.parser.registerCsiHandler({final:'M'},params=>scroll(params,true));
    term.parser.registerCsiHandler({final:'S'},params=>scroll(params,false));
    term.parser.registerCsiHandler({final:'J'},([mode])=>{
      const buffer=bufferService.buffer;
      if(!buffer.hasScrollback)return false;
      if(mode===3){
        archiveUntil?.(buffer.ybase);
        return false;
      }
      if(mode!==2&&(mode!==0||buffer.x!==0||buffer.y!==0))return false;
      // ED2 and ED0 at home clear the whole viewport, regardless of margins.
      // Use xterm's native scroll-on-erase with the entire viewport in scope.
      const {scrollTop,scrollBottom}=buffer;
      const scrollOnErase=term.options.scrollOnEraseInDisplay;
      buffer.scrollTop=0;
      buffer.scrollBottom=term.rows-1;
      term.options.scrollOnEraseInDisplay=true;
      try{return input.eraseInDisplay({params:[2]});}
      finally{
        term.options.scrollOnEraseInDisplay=scrollOnErase;
        buffer.scrollTop=scrollTop;
        buffer.scrollBottom=scrollBottom;
      }
    });
    term.parser.registerEscHandler({final:'c'},()=>{
      archiveUntil?.(term.buffer.normal.length);
      return false;
    });
  }
  function history(term,archive='',end=term.buffer.normal.length){
    const buffer=term.buffer.normal,lines=[];
    for(let i=Math.max(0,end-20000);i<end;i++){
      const line=buffer.getLine(i);
      if(!line)continue;
      const wraps=i+1<end&&!!buffer.getLine(i+1)?.isWrapped;
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
  function scrollLocal(term,lines){
    // Use the same buffer operation as xterm's scrollbar. It also works
    // when the DOM viewport is being resized or the terminal is read-only.
    term._core._bufferService.scrollLines(lines);
  }
  function programWheel(term,screen,options){
    let remainder=0;
    return event=>{
      const filter=options.filter();
      if(!options.canSend()||!filter.acceptsWheel()){remainder=0;return false;}
      const {left,top,width,height}=screen.getBoundingClientRect();
      const x=event.clientX-left,y=event.clientY-top;
      if(x<0||y<0||x>=width||y>=height)return false;
      const cellHeight=height/term.rows;
      const delta=event.deltaMode===1?event.deltaY:event.deltaMode===2?event.deltaY*term.rows:event.deltaY/cellHeight;
      remainder+=delta;
      const steps=Math.trunc(remainder);
      remainder-=steps;
      if(steps){
        const column=Math.floor(x*term.cols/width)+1,row=Math.floor(y*term.rows/height)+1;
        const report=filter.wheel(steps<0,column,row,Math.floor(x)+1,Math.floor(y)+1);
        options.send(report.repeat(Math.min(6,Math.abs(steps))));
      }
      return true;
    };
  }
  function installScroll(term,element,options){
    let remainder=0;
    const needsReview=()=>term.buffer.active.type==='alternate'||
      (term.buffer.active.viewportY===0&&options.hasArchive?.());
    const stop=event=>{event.preventDefault();event.stopPropagation();};
    const wheel=event=>{
      if(options.historyOpen()||event.ctrlKey||!event.deltaY)return;
      const {baseY,viewportY}=term.buffer.active;
      if(!event.shiftKey&&viewportY===baseY&&options.programWheel?.(event)){stop(event);return;}
      // Remote mouse is an explicit per-window opt-in. Shift always allows local review.
      if(options.remoteMouse()&&!event.shiftKey)return;
      const canReview=event.deltaY<0&&needsReview();
      const canScroll=event.deltaY<0?viewportY>0:viewportY<baseY;
      if(!canReview&&!canScroll&&options.nativePan?.(event)&&!event.shiftKey){event.stopPropagation();return;}
      stop(event);
      if(canReview){
        options.openHistory();
        return;
      }
      if(term.buffer.active.type==='alternate')return;
      const lineHeight=Math.max(1,options.lineHeight());
      const delta=event.deltaMode===1?event.deltaY:event.deltaMode===2?event.deltaY*term.rows:event.deltaY/lineHeight;
      remainder+=delta;
      const lines=Math.trunc(remainder);
      if(lines){scrollLocal(term,lines);remainder-=lines;options.changed();}
    };
    const key=event=>{
      if(event.type!=='keydown'||!event.shiftKey||event.ctrlKey||event.metaKey||event.altKey)return false;
      if(!['PageUp','PageDown','Home','End'].includes(event.key))return false;
      stop(event);
      if(event.key==='End'){options.live();return true;}
      if(event.key!=='PageDown'&&needsReview()){options.openHistory();return true;}
      const lines=event.key==='Home'?-term.buffer.active.viewportY:
        (term.rows-1)*(event.key==='PageUp'?-1:1);
      scrollLocal(term,lines);
      options.changed();return true;
    };
    element.addEventListener('wheel',wheel,{capture:true,passive:false});
    const subscription=term.onScroll(options.changed);
    return {key,dispose(){element.removeEventListener('wheel',wheel,true);subscription.dispose();}};
  }
  root.TerminalView={resolveTheme,palette,installScreenHistory,history,installScroll,scrollLocal,programWheel};
})(typeof window==='undefined'?globalThis:window);
