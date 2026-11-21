import unittest
from tz_mcp.input_bridge import CheckedInput,staged_command
from unittest.mock import patch
import tempfile
from pathlib import Path
from types import SimpleNamespace

class Fake:
    def __init__(self):self.valid=True;self.keys=[]
    def focused(self,pid,hwnd):return self.valid
    def unicode(self,ch):
        self.keys.append(ch)
        if ch=='1':self.valid=False
    def enter(self):self.keys.append('ENTER')

class InputTests(unittest.TestCase):
    def test_loss_after_posting_requests_command_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'command.log';p.write_text('')
            class Doc:
                def GetVariable(self,k):return str(p) if k=='LOGFILENAME' else 0
                def SetVariable(self,k,v):pass
                def PostCommand(self,text):p.write_text(text)
            b=Fake();b.activate_view=lambda pid,hwnd:None
            c=SimpleNamespace(pid=1,hwnd=2,guard=lambda:None,doc=Doc())
            with patch('tz_mcp.input_bridge.NativeInput',return_value=b):
                with self.assertRaises(Exception) as caught:staged_command(c,'TGColumn',['120,0',''])
            self.assertTrue(getattr(caught.exception,'command_completion_timeout',False))
    def test_rejects_wrong_focus_before_any_key(self):
        b=Fake();b.valid=False
        with self.assertRaises(ValueError):CheckedInput(1,2,b).line('200,0')
        self.assertEqual([],b.keys)
    def test_stops_immediately_if_focus_changes_mid_input(self):
        b=Fake()
        with self.assertRaises(ValueError):CheckedInput(1,2,b).line('120,0')
        self.assertEqual(['1'],b.keys)
    def test_empty_input_only_submits_one_enter(self):
        b=Fake();CheckedInput(1,2,b).line('');self.assertEqual(['ENTER'],b.keys)

if __name__=='__main__':unittest.main()
