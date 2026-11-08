/* xterm owns keyboard and mouse encoding. Preserve its two input channels. */
(function(root){
  'use strict';
  function install(term,send){
    const text=term.onData(data=>send({type:'input',data}));
    const binary=term.onBinary(data=>send({type:'input',data,binary:true}));
    return {dispose(){text.dispose();binary.dispose();}};
  }
  root.TerminalInput={install};
})(typeof window==='undefined'?globalThis:window);
