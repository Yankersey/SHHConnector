"""Integration tests use local SFTP and isolated GNU screen, never a real server."""
import base64
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import transfers
from ssh_config import read_profiles


def wait_for(predicate, timeout=15):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():
            return
        time.sleep(.04)
    raise AssertionError('Condition timed out')


class CaptureSocket:
    def __init__(self):
        self.output=bytearray()
        self.events=[]
    def send_frame(self, opcode, data):
        self.output.extend(data)
    def send_json(self, data):
        self.events.append(data)


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=Path(tempfile.mkdtemp(prefix='screendesk-tests-'))
        candidates=[os.environ.get('SCREENDESK_SFTP_SERVER',''),'/usr/libexec/sftp-server','/usr/lib/openssh/sftp-server','/usr/lib/ssh/sftp-server']
        sftp_server=next((str(Path(path).resolve()) for path in candidates if path and Path(path).is_file()),None)
        if not sftp_server:
            shutil.rmtree(cls.folder)
            raise unittest.SkipTest('Install openssh-sftp-server or set SCREENDESK_SFTP_SERVER')
        cls.fake_ssh=cls.folder/'ssh'
        cls.fake_ssh.write_text('#!'+sys.executable+'\nimport os,sys,subprocess\n'
          'if "-O" in sys.argv: sys.exit(0)\n'
          'if "-s" in sys.argv: os.execv('+repr(sftp_server)+',["sftp-server"])\n'
          'sys.exit(subprocess.call(["/bin/sh","-c",sys.argv[-1]]))\n')
        cls.fake_ssh.chmod(0o700)
        cls.original_ssh=app.SSH
        app.SSH=str(cls.fake_ssh)

    @classmethod
    def tearDownClass(cls):
        app.SSH=cls.original_ssh
        import shutil
        shutil.rmtree(cls.folder)

    def setUp(self):
        self.temp=Path(tempfile.mkdtemp(dir=str(self.folder)))
        self.ws=app.Workspace(self.temp/'data',ssh_config=False)
        self.profile=app.clean_profile({'host':'isolated-test','name':'测试服务器'})
        self.ws.profiles.append(self.profile)
        self.remote=self.temp/'remote'
        self.remote.mkdir()

    def tearDown(self):
        self.ws.shutdown()

    def session(self):
        return self.ws.new_session({'name':'测试训练','profile_id':self.profile['id']})

    def test_01_input_validation_and_forwarding(self):
        with self.assertRaises(app.UserError):
            app.clean_profile({'host':'-oProxyCommand=bad'})
        with self.assertRaises(app.UserError):
            self.ws.new_session({'name':'','profile_id':self.profile['id']})
        a=self.ws.ssh_args(self.profile,forwarding=False)
        b=self.ws.ssh_args(self.profile,forwarding=True)
        self.assertIn('ClearAllForwardings=yes',a)
        self.assertIn('ClearAllForwardings=no',b)
        self.assertNotEqual(next(x for x in a if x.startswith('ControlPath=')),next(x for x in b if x.startswith('ControlPath=')))
        self.ws.activate(self.profile['id'],False);self.ws.activate(self.profile['id'],True)
        self.assertEqual(self.ws.connections[self.profile['id']]['policies'],[False,True])
        self.ws.require_active(self.profile['id'],False);self.ws.require_active(self.profile['id'],True)
        self.assertIn('ClearAllForwardings=yes',self.ws.ssh_args(self.profile,True,True))
        self.assertNotIn('screen -ls |',app.remote_script(self.session(),True,'token'))

    def test_02_ready_filter_at_every_split(self):
        prompt=app.ReadyFilter('token',lambda ref:None)
        self.assertEqual(prompt.feed(b'Password: '),b'Password: ')
        self.assertEqual(prompt.feed(b'Verification code: '),b'Verification code: ')
        raw=b'hello\x1eSD:token:123.sd_test\x1fworld'
        for split in range(len(raw)+1):
            refs=[]; f=app.ReadyFilter('token',refs.append)
            output=f.feed(raw[:split])+f.feed(raw[split:])+f.feed(b'',final=True)
            self.assertEqual(output,b'helloworld')
            self.assertEqual(refs,['123.sd_test'])

    def test_03_lazy_directory_pagination(self):
        for index in range(503):
            (self.remote/('log_%04d.log'%index)).touch()
        child=self.remote/'nested';child.mkdir();(child/'not-traversed.txt').touch()
        result=self.ws.transfers.list_directory(self.profile,str(self.remote),False)
        all_entries=list(result['entries']);self.assertEqual(len(all_entries),200)
        while result['more']:
            result=self.ws.transfers.list_directory(self.profile,str(self.remote),False,result['cursor'])
            self.assertLessEqual(len(result['entries']),200)
            all_entries+=result['entries']
        self.assertEqual(len(all_entries),504)
        self.assertNotIn('not-traversed.txt',[e['name'] for e in all_entries])

    def test_04_download_auto_resume_after_drop(self):
        source=self.remote/'network drop.bin';payload=os.urandom(4*1024*1024+321);source.write_bytes(payload)
        original=transfers.SFTP.read_block;dropped=threading.Event();offsets=[]
        def flaky(client,handle,offset,size):
            offsets.append(offset)
            if offset>=1024*1024 and not dropped.is_set():
                dropped.set();client.process.kill()
            return original(client,handle,offset,size)
        with mock.patch.object(transfers.SFTP,'read_block',flaky):
            job=self.ws.transfers.create(self.profile,str(source),False)
            actual=next(j for j in self.ws.transfers.jobs if j['id']==job['id'])
            wait_for(lambda:actual['state']=='completed',20)
        self.assertTrue(dropped.is_set())
        self.assertEqual(Path(actual['local_path']).read_bytes(),payload)
        self.assertGreaterEqual(offsets.count(0),2)
        self.assertFalse(Path(actual['part_path']).exists())

    def test_05_pause_restart_and_changed_source(self):
        source=self.remote/'pause.bin';source.write_bytes(os.urandom(12*1024*1024))
        original=transfers.SFTP.read_block
        def slow(*args):
            time.sleep(.025);return original(*args)
        with mock.patch.object(transfers.SFTP,'read_block',slow):
            job=self.ws.transfers.create(self.profile,str(source),False)
            actual=self.ws.transfers.jobs[-1]
            wait_for(lambda:actual['downloaded']>=1024*1024)
            self.ws.transfers.action(job['id'],'pause')
            wait_for(lambda:not self.ws.transfers.workers[job['id']][0].is_alive())
        self.assertEqual(actual['state'],'paused')
        self.assertGreater(Path(actual['part_path']).stat().st_size,0)
        # Recreate the app's manager from durable state, then resume the same prefix.
        self.ws.transfers.shutdown();self.ws.transfers=transfers.TransferManager(self.ws)
        actual=self.ws.transfers.jobs[-1]
        source.write_bytes(b'x'*source.stat().st_size)
        self.ws.transfers.action(job['id'],'resume')
        wait_for(lambda:actual['state']=='changed')
        wait_for(lambda:not self.ws.transfers.workers[job['id']][0].is_alive())
        self.assertTrue(Path(actual['part_path']).exists())
        self.ws.transfers.action(job['id'],'restart')
        wait_for(lambda:actual['state']=='completed',20)
        self.assertEqual(Path(actual['local_path']).read_bytes(),source.read_bytes())
        self.assertTrue(list(Path(actual['part_path']).parent.glob('*.saved-*')))

    def test_06_http_auth_and_shutdown_do_not_end_screen(self):
        server=app.Server(0,self.ws);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            client=http.client.HTTPConnection('127.0.0.1',server.server_port)
            client.request('GET','/api/workspace');response=client.getresponse();self.assertEqual(response.status,403);response.read()
            client.request('GET','/api/workspace',headers={'X-ScreenDesk-Token':self.ws.token});response=client.getresponse();self.assertEqual(response.status,200);response.read()
            client.request('POST','/api/shutdown',body='{}',headers={'Content-Type':'application/json','X-ScreenDesk-Token':self.ws.token,'Origin':'https://evil.example'});response=client.getresponse();self.assertEqual(response.status,403);response.read();client.close()
        finally:
            server.shutdown();server.server_close()

    def test_09_switch_workspaces_and_close_server_choices(self):
        first=self.ws.record(self.session()['id']);second=self.ws.record(self.session()['id'])
        other=app.clean_profile({'host':'other-isolated-test','name':'另一台服务器'})
        self.ws.profiles.append(other)
        third=self.ws.record(self.ws.new_session({'name':'other task','profile_id':other['id']})['id'])
        class StubTerminal:
            def __init__(self,record):self.record=record;self.stopped=False;self.closing=False
            def event(self,**kwargs):pass
            def stop(self):self.stopped=True
        for record in (first,second,third):
            record.update(ever_ready=True,state='connected',screen_ref='123.'+record['remote_name'],layout={'x':45,'y':71,'width':510,'height':330,'visible':True})
            self.ws.terminals[record['id']]=StubTerminal(record)
        self.ws.activate(self.profile['id'],False);self.ws.activate(other['id'],True)
        self.assertEqual(len(self.ws.connections),2)
        self.assertFalse(any(t.stopped for t in self.ws.terminals.values()))
        with mock.patch.object(self.ws,'control') as control:
            self.ws.close_server(self.profile['id'],{first['id']:'detach',second['id']:'terminate'})
            self.assertEqual(control.call_count,1)
            self.assertEqual(control.call_args.args[1],'terminate')
        self.assertNotIn(second['id'],[r['id'] for r in self.ws.snapshot()['sessions']])
        self.assertTrue(first['layout']['visible']);self.assertEqual(first['layout']['x'],45)
        self.assertEqual(first['state'],'detached')
        self.assertFalse(self.ws.terminals[third['id']].stopped)
        self.assertIn(other['id'],self.ws.connections)
        self.assertNotIn(self.profile['id'],self.ws.connections)
        restored=app.Workspace(self.temp/'data',ssh_config=False)
        try:
            retained=restored.record(first['id'])
            self.assertEqual(retained['state'],'detached');self.assertTrue(retained['layout']['visible'])
            self.assertEqual(retained['layout']['width'],510)
            self.assertFalse(restored.terminals)
            self.assertNotIn(second['id'],[r['id'] for r in restored.snapshot()['sessions']])
        finally:restored.shutdown()

    def test_10_ssh_config_aliases_and_includes(self):
        included=self.temp/'included.conf'
        included.write_text('Host gamma delta\n HostName example.invalid\nHost !exclude *.wild\n')
        config=self.temp/'config'
        config.write_text('Host alpha beta\n Port 2200\nHost *\n ServerAliveInterval 15\nInclude "'+str(included)+'"\nInclude "'+str(included)+'"\nHost=epsilon # comment\nMatch exec "do-not-execute"\n')
        profiles,notes=read_profiles(config)
        self.assertEqual([p['host'] for p in profiles],['alpha','beta','gamma','delta','epsilon'])
        self.assertFalse(notes);self.assertTrue(all(p['source']=='ssh_config' for p in profiles))
        self.assertEqual(read_profiles(config)[0],profiles)

    @unittest.skipUnless(os.environ.get('SCREENDESK_TEST_SCREEN')=='1','requires real screen')
    def test_11_slow_display_does_not_block_job_or_screen_socket(self):
        directory=Path(tempfile.mkdtemp(prefix='sds-',dir='/tmp'))
        previous=os.environ.get('SCREENDIR');os.environ['SCREENDIR']=str(directory)
        record=self.ws.record(self.session()['id']);captured=CaptureSocket()
        terminal=app.Terminal(self.ws,record,captured,100,30);self.ws.terminals[record['id']]=terminal
        marker=self.temp/'output-finished'
        try:
            terminal.start();wait_for(lambda:record['state']=='connected')
            command="import sys; from pathlib import Path; sys.stdout.write('X'*8388608); sys.stdout.flush(); Path(%r).touch()"%str(marker)
            import shlex
            terminal.write('python3 -c '+shlex.quote(command)+'\n')
            # CaptureSocket deliberately never acknowledges output: remote display
            # is backpressured after 512 KiB, but the job must still finish.
            wait_for(marker.exists,8)
            result=subprocess.run(['screen','-ls',record['screen_ref']],capture_output=True,text=True,timeout=3)
            self.assertIn(record['remote_name'],result.stdout)
            self.assertLessEqual(len(captured.output),600*1024)
        finally:
            terminal.stop();subprocess.run(['screen','-S',record['remote_name'],'-X','quit'],capture_output=True,timeout=4)
            if previous is None:os.environ.pop('SCREENDIR',None)
            else:os.environ['SCREENDIR']=previous

    def test_12_remote_control_watchdog_and_queue_bound(self):
        result=subprocess.run(['/bin/sh','-c',app.remote_deadline_shell()+"sd_run python3 -c 'import time; time.sleep(30)'"],capture_output=True,text=True,timeout=10)
        self.assertIn(result.returncode,(124,137,143))
        self.assertIn('6 秒',result.stderr)
        class Socket:
            def shutdown(self,*args):pass
        class Handler:
            rfile=None
            connection=Socket()
        ws=app.WebSocket(Handler())
        with self.assertRaises(OSError):
            for _ in range(100):ws.send_frame(2,b'X'*32768)
        self.assertTrue(ws.closed);self.assertLessEqual(ws.buffered,1024*1024)

    def test_13_check_distinguishes_missing_from_unreachable(self):
        record=self.ws.record(self.session()['id'])
        record.update(ever_ready=True,state='detached',screen_ref='123.'+record['remote_name'])
        for code,output in [(255,b'Permission denied'),(124,b'timed out'),
                            (1,('There is a screen on:\n\t'+record['screen_ref']+'\t(Dead ???)\n').encode())]:
            with mock.patch.object(self.ws,'run_ssh',return_value=subprocess.CompletedProcess([],code,output)):
                result=self.ws.check_session(record['id'])
            self.assertEqual(result['status'],'unknown');self.assertEqual(record['state'],'detached')
        found=('There is a screen on:\n\t'+record['screen_ref']+'\t(Detached)\n').encode()
        with mock.patch.object(self.ws,'run_ssh',return_value=subprocess.CompletedProcess([],0,found)):
            self.assertEqual(self.ws.check_session(record['id'])['status'],'exists')
        with mock.patch.object(self.ws,'run_ssh',return_value=subprocess.CompletedProcess([],1,b'No Sockets found in /run/screen/S-test.\n')):
            result=self.ws.check_session(record['id'])
        self.assertEqual(result['status'],'missing');self.assertEqual(record['state'],'ended')
        self.assertNotIn(record['id'],[r['id'] for r in self.ws.snapshot()['sessions']])

    @unittest.skipUnless(os.environ.get('SCREENDESK_TEST_SCREEN')=='1','requires isolated real screen')
    def test_15_two_workspaces_discover_the_same_server_names(self):
        directory=Path(tempfile.mkdtemp(prefix='sds-',dir='/tmp'))
        previous=os.environ.get('SCREENDIR');os.environ['SCREENDIR']=str(directory)
        other=app.Workspace(self.temp/'other-machine'/'data',ssh_config=False)
        other.profiles.append(dict(self.profile))
        other.activate(self.profile['id'],False)
        record=self.ws.record(self.session()['id'])
        self.ws.activate(self.profile['id'],False)
        try:
            subprocess.run(['screen','-U','-d','-m','-S',record['remote_name'],'sh','-c','sleep 60'],check=True)
            wait_for(lambda:record['remote_name'].encode() in subprocess.run(['screen','-ls'],capture_output=True).stdout)
            scanned=other.scan_screens(self.profile['id'],False)
            entry=next(e for e in scanned['entries'] if e['name']==record['name'])
            self.assertTrue(entry['managed']);self.assertEqual(entry['state'],'detached')
            added=other.add_discovered_screen(dict(profile_id=self.profile['id'],scan_id=scanned['scan_id'],screen_ref=entry['screen_ref']))
            self.assertEqual(added['name'],record['name']);self.assertEqual(added['state'],'detached')
            self.assertFalse(other.terminals)
            other.forget_session(added['id'])
            self.assertIn(record['remote_name'].encode(),subprocess.run(['screen','-ls'],capture_output=True).stdout)
        finally:
            other.shutdown()
            subprocess.run(['screen','-S',record['remote_name'],'-X','quit'],capture_output=True)
            if previous is None:os.environ.pop('SCREENDIR',None)
            else:os.environ['SCREENDIR']=previous
            shutil.rmtree(directory,ignore_errors=True)

    def test_14_forget_active_record_detaches_without_remote_quit(self):
        record=self.ws.record(self.session()['id'])
        class StubTerminal:
            stopped=False
            def event(self,**kwargs):pass
            def stop(self):self.stopped=True
        terminal=StubTerminal();self.ws.terminals[record['id']]=terminal
        with mock.patch.object(self.ws,'control') as control:
            self.ws.forget_session(record['id'])
            control.assert_not_called()
        wait_for(lambda:terminal.stopped)
        self.assertNotIn(record['id'],[r['id'] for r in self.ws.records])
        restored=app.Workspace(self.temp/'data',ssh_config=False)
        try:self.assertNotIn(record['id'],[r['id'] for r in restored.records])
        finally:restored.shutdown()

    @unittest.skipUnless(os.environ.get('SCREENDESK_TEST_SCREEN')=='1','requires local socket and real screen')
    def test_08_websocket_input_and_unexpected_socket_drop(self):
        directory=Path(tempfile.mkdtemp(prefix='sds-',dir='/tmp'))
        previous=os.environ.get('SCREENDIR');os.environ['SCREENDIR']=str(directory)
        record=self.ws.record(self.session()['id'])
        self.ws.activate(self.profile['id'],False)
        server=app.Server(0,self.ws);threading.Thread(target=server.serve_forever,daemon=True).start()
        sock=socket.create_connection(('127.0.0.1',server.server_port));sock.settimeout(10)
        reader=sock.makefile('rb')
        try:
            key=base64.b64encode(os.urandom(16)).decode()
            request=('GET /ws/%s?forwarding=0 HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: %s\r\nOrigin: %s\r\nSec-WebSocket-Protocol: screen-desk, %s\r\n\r\n')%(record['id'],server.server_port,key,server.origin,self.ws.token)
            sock.sendall(request.encode())
            self.assertIn(b'101',reader.readline())
            while reader.readline()!=b'\r\n':pass
            wait_for(lambda:record['state']=='connected')
            payload=json.dumps(dict(type='input',data="printf 'SOCKET%sOK\\n' '-'\n")).encode()
            mask=os.urandom(4)
            sock.sendall(bytes([0x81,0x80|len(payload)])+mask+bytes(b^mask[i%4] for i,b in enumerate(payload)))
            output=bytearray()
            while b'SOCKET-OK' not in output:
                a,b=reader.read(2);length=b&127
                if length==126:length=struct.unpack('!H',reader.read(2))[0]
                elif length==127:length=struct.unpack('!Q',reader.read(8))[0]
                data=reader.read(length)
                if a&15==2:output.extend(data)
            reader.close();sock.close()
            wait_for(lambda:self.ws.terminals[record['id']].stopped)
            listing=subprocess.run(['screen','-ls'],capture_output=True,text=True).stdout
            self.assertIn(record['remote_name'],listing)
            self.ws.close_session(record['id'],'terminate')
        finally:
            reader.close();sock.close();server.shutdown();server.server_close()
            subprocess.run(['screen','-S',record['remote_name'],'-X','quit'],capture_output=True)
            if previous is None:os.environ.pop('SCREENDIR',None)
            else:os.environ['SCREENDIR']=previous

    @unittest.skipUnless(os.environ.get('SCREENDESK_TEST_SCREEN')=='1','set SCREENDESK_TEST_SCREEN=1 for real screen PTY lifecycle')
    def test_07_real_screen_survives_drop_then_explicit_quit(self):
        directory=Path(tempfile.mkdtemp(prefix='sds-',dir='/tmp'))
        previous=os.environ.get('SCREENDIR');os.environ['SCREENDIR']=str(directory)
        record=self.ws.record(self.session()['id'])
        terminals=[]
        try:
            for attempt in range(3):
                captured=CaptureSocket();terminal=app.Terminal(self.ws,record,captured,100,30)
                self.ws.terminals[record['id']]=terminal;terminals.append(terminal);terminal.start()
                try:
                    wait_for(lambda:record['state']=='connected')
                except AssertionError:
                    listing=subprocess.run(['screen','-ls'],capture_output=True,text=True).stdout
                    pid=terminal.process.pid;details=[]
                    for _ in range(3):
                        details.append(subprocess.run(['ps','-o','pid,ppid,pgid,tpgid,stat,wchan,comm','-p',str(pid)],capture_output=True,text=True).stdout)
                        child=subprocess.run(['pgrep','-P',str(pid)],capture_output=True,text=True).stdout.split()
                        if not child:break
                        pid=int(child[0])
                    self.fail('attempt=%s state=%s events=%r output=%r listing=%r processes=%r' % (attempt,record['state'],captured.events,bytes(captured.output),listing,details))
                if attempt==0:
                    terminal.write("SD_PERSISTED=still-here\nprintf 'INITIAL%sOK\\n' '-'\n")
                    wait_for(lambda:b'INITIAL-OK' in captured.output)
                    terminal.stop() # Simulate socket loss, without a screen command.
                elif attempt==1:
                    terminal.write("printf 'VALUE=%s\\n' \"$SD_PERSISTED\"\n")
                    try:
                        wait_for(lambda:b'VALUE=still-here' in captured.output)
                    except AssertionError:
                        self.fail('restore output=%r events=%r' % (bytes(captured.output),captured.events))
                    self.ws.close_session(record['id'],'detach')
                else:
                    self.ws.close_session(record['id'],'terminate')
                wait_for(lambda:terminal.stopped)
                time.sleep(.15)
                listing=subprocess.run(['screen','-ls'],capture_output=True,text=True).stdout
                self.assertEqual(record['remote_name'] in listing,attempt<2)
            self.assertEqual(record['state'],'ended')
        finally:
            for terminal in terminals:terminal.stop()
            subprocess.run(['screen','-S',record['remote_name'],'-X','quit'],capture_output=True)
            if previous is None:os.environ.pop('SCREENDIR',None)
            else:os.environ['SCREENDIR']=previous


if __name__=='__main__':
    unittest.main(verbosity=2)
