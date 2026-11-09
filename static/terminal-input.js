/* xterm owns keyboard and mouse encoding. Preserve its two input channels. */
(function(root){
  'use strict';
  function install(term,send){
    const text=term.onData(data=>send({type:'input',data}));
    const binary=term.onBinary(data=>send({type:'input',data,binary:true}));
    return {dispose(){text.dispose();binary.dispose();}};
  }
  function clipboardKey(term,event,options){
    if(!event.ctrlKey||event.altKey||event.metaKey)return true;
    const key=event.code||('Key'+String(event.key||'').toUpperCase());
    if(key==='KeyC'&&(event.shiftKey||(options.windows&&term.hasSelection()))){
      event.preventDefault();
      if(event.type==='keydown')options.copy();
      return false;
    }
    // Let the browser deliver a native paste event to xterm. Encoding Ctrl+V
    // here would send SYN (0x16) and cancel the browser's clipboard shortcut.
    if(key==='KeyV'&&(options.windows||event.shiftKey))return false;
    return true;
  }
  root.TerminalInput={install,clipboardKey};
})(typeof window==='undefined'?globalThis:window);
