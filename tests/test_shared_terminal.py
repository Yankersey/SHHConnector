import threading
import time
import unittest
from unittest import mock

import shared_terminal as shared


def wait_for(predicate):
    end=time.monotonic()+4
    while time.monotonic()<end:
        if predicate():return
        time.sleep(.01)
    raise AssertionError("Shared viewer timed out")


class Socket:
    def __init__(self,ack=True):
        self.closed=False;self.buffered=0;self.frames=[];self.messages=[];self.peer=None;self.auto_ack=ack
    def send_frame(self,opcode,data):
        if self.closed:raise OSError("closed")
        self.frames.append(data)
        if self.auto_ack:self.peer.ack(len(data)-9)
    def send_json(self,data):
        if self.closed:raise OSError("closed")
        self.messages.append(dict(data))
    def abort(self):self.closed=True
    def text(self):return b''.join(frame[9:] for frame in self.frames)


class Terminal:
    def __init__(self):
        self.record=dict(state='connected',forwarding=False,screen_ref='123.fixture');self.writes=[];self.sizes=[];self.stopped=False
    def write(self,text):self.writes.append(text)
    def resize(self,cols,rows):self.sizes.append((cols,rows))
    def stop(self):self.stopped=True


class SharedTerminalTests(unittest.TestCase):
    def setUp(self):
        self.hub=shared.SharedTerminal(80,24);self.hub.terminal=Terminal()
    def tearDown(self):
        self.hub.finish('detached')
        for peer in list(self.hub.viewers.values()):self.hub.remove(peer.id)
    def viewer(self,kind='desktop',ack=True):
        socket=Socket(ack);peer=self.hub.add(socket,kind);socket.peer=peer;peer.start();wait_for(lambda:peer.ready);return peer,socket

    def test_two_views_one_operator_transfer_and_individual_disconnect(self):
        owner,a=self.viewer();self.hub.send_frame(2,'中文第一帧\r\n'.encode())
        observer,b=self.viewer('mobile');wait_for(lambda:'中文第一帧'.encode() in b.text())
        self.hub.input(observer,'REJECT');self.hub.resize(observer,10,5)
        self.assertFalse(self.hub.terminal.writes);self.assertFalse(self.hub.terminal.sizes)
        self.hub.input(owner,'ONE');self.assertEqual(self.hub.terminal.writes,['ONE'])
        self.hub.control(observer);self.hub.input(owner,'REJECT');self.hub.input(observer,'TWO')
        self.hub.resize(observer,42,28);self.assertEqual(self.hub.terminal.sizes,[(42,28)])
        self.assertEqual(self.hub.terminal.writes,['ONE','TWO'])
        self.assertFalse(a.closed);self.assertFalse(b.closed)
        self.hub.send_frame(2,'第二帧🙂'.encode());wait_for(lambda:'第二帧🙂'.encode() in a.text() and '第二帧🙂'.encode() in b.text())
        self.hub.remove(owner.id);self.assertFalse(self.hub.terminal.stopped)
        self.hub.input(observer,'STILL');self.assertEqual(self.hub.terminal.writes[-1],'STILL')
        self.hub.remove(observer.id);self.assertTrue(self.hub.terminal.stopped)

    def test_operator_disconnect_leaves_other_view_read_only_until_claimed(self):
        first,a=self.viewer();second,b=self.viewer('mobile')
        self.hub.remove(first.id);self.assertIsNone(self.hub.owner);self.assertFalse(self.hub.terminal.stopped)
        self.hub.input(second,'REJECT');self.assertFalse(self.hub.terminal.writes)
        self.hub.control(second);self.hub.input(second,'OK');self.assertEqual(self.hub.terminal.writes,['OK'])
        self.hub.control(second,True);self.assertIsNone(self.hub.owner)

    def test_slow_observer_does_not_block_owner_or_remote_job(self):
        first,a=self.viewer()
        with mock.patch.object(shared,'VIEW_QUEUE_LIMIT',256),mock.patch.object(shared,'OUTPUT_CREDIT',64):
            second,b=self.viewer('mobile',False)
            # Give the fast reader enough scheduling time; the stalled one has
            # bounded credit and cannot accumulate unbounded output.
            for i in range(50):
                self.hub.send_frame(2,b'x'*32);time.sleep(.002)
            wait_for(lambda:second.id not in self.hub.viewers)
        self.assertFalse(a.closed);self.assertTrue(b.closed);self.assertFalse(self.hub.terminal.stopped)
        self.hub.input(first,'CONTINUE');self.assertEqual(self.hub.terminal.writes,['CONTINUE'])

    def test_burst_pauses_for_checkpoint_and_resumes_on_confirmation(self):
        first, socket = self.viewer()
        with mock.patch.object(shared, 'CHECKPOINT_AT', 1), mock.patch.object(shared, 'JOURNAL_LIMIT', 256 * 1024):
            self.hub.send_frame(2, b'a' * (128 * 1024))
            self.assertFalse(self.hub.output_ready())
            wait_for(lambda: any(event['type'] == 'snapshot_request' for event in socket.messages))
            request = next(event for event in socket.messages if event['type'] == 'snapshot_request')
            self.hub.snapshot(first, dict(request, text='retained screen'))
            self.assertTrue(self.hub.output_ready())
            self.assertFalse(first.closed)

    def test_checkpoint_exact_sequence_unicode_boundary_and_late_join(self):
        first,a=self.viewer()
        with mock.patch.object(shared,'CHECKPOINT_AT',1):
            self.hub.send_frame(2,b'\x1b[31');self.assertIsNone(self.hub.snapshot_request)
            utf='中文🙂'.encode();self.hub.send_frame(2,b'm'+utf[:-1]);self.assertIsNone(self.hub.snapshot_request)
            self.hub.send_frame(2,utf[-1:]);request=self.hub.snapshot_request;self.assertIsNotNone(request)
            self.hub.send_frame(2,b'AFTER')
            message=dict(nonce=request[1],seq=request[2],cols=80,rows=24,text='\x1b[H中文🙂')
            self.hub.snapshot(first,{**message,'seq':999});self.assertFalse(self.hub.checkpoint)
            self.hub.snapshot(first,message)
            self.assertEqual(self.hub.checkpoint,'\x1b[H中文🙂'.encode())
            second,b=self.viewer('mobile');wait_for(lambda:b'AFTER' in b.text())
            self.assertEqual(b.text(),'\x1b[H中文🙂AFTER'.encode())

    def test_mobile_revocation_keeps_desktop_view_and_drops_mobile_owner(self):
        first,a=self.viewer();second,b=self.viewer('mobile');self.hub.control(second)
        self.hub.disconnect_kind('mobile')
        self.assertTrue(b.closed);self.assertFalse(a.closed);self.assertIsNone(self.hub.owner);self.assertFalse(self.hub.terminal.stopped)
        self.hub.control(first);self.hub.input(first,'PC');self.assertEqual(self.hub.terminal.writes,['PC'])

    def test_binary_mouse_and_unicode_keyboard_remain_distinct(self):
        first, socket = self.viewer()
        second, observer = self.viewer('mobile')
        self.hub.input(first, '草稿写到一半')
        self.hub.input(first, '\x1b[M\x60\x99\x29', binary=True)
        self.hub.input(first, '\x1b[1;2D')
        self.hub.input(first, '继续打字')
        self.assertEqual(self.hub.terminal.writes, [
            '草稿写到一半', b'\x1b[M\x60\x99\x29', '\x1b[1;2D', '继续打字'])
        self.hub.input(second, '\x1b[M\x60\x99\x29', binary=True)
        self.assertEqual(len(self.hub.terminal.writes), 4)
        with self.assertRaises(ValueError):
            self.hub.input(first, '中文', binary=True)

    def test_unsupported_binary_input_leaves_keyboard_connection_usable(self):
        first, socket = self.viewer()
        with mock.patch.object(self.hub.terminal, "write", side_effect=UnicodeDecodeError("ascii",b"\x99",0,1,"legacy mouse")):
            self.hub.input(first, '\x1b[M\x60\x99\x29', binary=True)
        self.hub.input(first,"继续打字")
        self.assertEqual(self.hub.terminal.writes,["继续打字"])
        self.assertFalse(first.closed)

    def test_simultaneous_claims_have_one_input_owner(self):
        first,a=self.viewer();second,b=self.viewer('mobile')
        threads=[threading.Thread(target=self.hub.control,args=(peer,)) for peer in [first,second]*20]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(sum(self.hub.allows(p.id) for p in (first,second)),1)


class BoundaryTests(unittest.TestCase):
    def test_utf8_controls_and_string_boundaries_at_every_byte_split(self):
        for raw in ['中文🙂'.encode(),b'\x1b[38;2;1;2;3m',b'\x1b(B',b'\x1b]0;title\x07',b'\x1bP1;2qpayload\x1b\\',b'\x1b_title\x1b\\']:
            for split in range(1,len(raw)):
                boundary=shared.VTBoundary();boundary.feed(raw[:split]);boundary.feed(raw[split:]);self.assertTrue(boundary.safe,(raw,split))
        boundary=shared.VTBoundary();boundary.feed(b'\x1b]0;partial');self.assertFalse(boundary.safe)
        boundary.feed(b'\x18');self.assertTrue(boundary.safe)
