"""PID/focus-checked native input, explicitly enabled by the user for Tianzheng.

Uses the staged PostCommand + foreground keyboard pattern documented by
Yzm889/TianZheng-ai-plugin (MIT), with checks before every character/Enter.
"""
import ctypes
import time
from ctypes import wintypes as W
from pathlib import Path


class CheckedInput:
    def __init__(self,pid,hwnd,backend):self.pid,self.hwnd,self.backend=pid,hwnd,backend
    def check(self):
        if not self.backend.focused(self.pid,self.hwnd):raise ValueError('CAD PID/view focus lost; no further input sent')
    def line(self,text):
        self.check()
        for ch in text:
            self.check();self.backend.unicode(ch)
        self.check();self.backend.enter()


class NativeInput:
    def __init__(self):
        self.u=ctypes.WinDLL('user32',use_last_error=True);self.k=ctypes.WinDLL('kernel32')
        self.u.GetForegroundWindow.restype=W.HWND
        self.u.GetWindowThreadProcessId.argtypes=[W.HWND,ctypes.POINTER(W.DWORD)]
        self.u.SetForegroundWindow.argtypes=[W.HWND]
        self.u.SetFocus.argtypes=[W.HWND];self.u.SetFocus.restype=W.HWND
        self.u.GetWindowRect.argtypes=[W.HWND,ctypes.POINTER(W.RECT)]
        self.u.GetClassNameW.argtypes=[W.HWND,W.LPWSTR,ctypes.c_int]
        self.u.IsChild.argtypes=[W.HWND,W.HWND]
        self.callback=ctypes.WINFUNCTYPE(W.BOOL,W.HWND,W.LPARAM)
        class KEY(ctypes.Structure):
            _fields_=[('vk',W.WORD),('scan',W.WORD),('flags',W.DWORD),('time',W.DWORD),('extra',ctypes.c_size_t)]
        class MOUSE(ctypes.Structure):
            _fields_=[('x',W.LONG),('y',W.LONG),('data',W.DWORD),('flags',W.DWORD),('time',W.DWORD),('extra',ctypes.c_size_t)]
        class UNION(ctypes.Union):_fields_=[('key',KEY),('mouse',MOUSE)]
        class INPUT(ctypes.Structure):_fields_=[('type',W.DWORD),('data',UNION)]
        self.KEY,self.INPUT=KEY,INPUT
        self.u.SendInput.argtypes=[W.UINT,ctypes.POINTER(INPUT),ctypes.c_int]
        self.view=None
    def pid(self,hwnd):
        pid=W.DWORD();self.u.GetWindowThreadProcessId(W.HWND(hwnd),ctypes.byref(pid));return pid.value
    def focus_handle(self,hwnd):
        class INFO(ctypes.Structure):
            _fields_=[('size',W.DWORD),('flags',W.DWORD),('active',W.HWND),('focus',W.HWND),('capture',W.HWND),('menu',W.HWND),('move',W.HWND),('caret',W.HWND),('rect',W.RECT)]
        info=INFO();info.size=ctypes.sizeof(info);thread=self.u.GetWindowThreadProcessId(W.HWND(hwnd),None)
        return int(info.focus or 0) if self.u.GetGUIThreadInfo(thread,ctypes.byref(info)) else 0
    def focused(self,pid,hwnd):
        if self.pid(hwnd)!=pid or int(self.u.GetForegroundWindow() or 0)!=hwnd:return False
        focus=self.focus_handle(hwnd)
        return bool(focus and self.pid(focus)==pid and self.view and
                    (focus==self.view or self.u.IsChild(W.HWND(self.view),W.HWND(focus)) or
                     (focus!=hwnd and self.u.IsChild(W.HWND(focus),W.HWND(self.view)))))
    def activate_view(self,pid,hwnd):
        if self.pid(hwnd)!=pid:raise ValueError('CAD process identity changed')
        candidates=[]
        def visit(child,_):
            name=ctypes.create_unicode_buffer(256);self.u.GetClassNameW(child,name,256)
            if self.pid(int(child))==pid and self.u.IsWindowVisible(child) and any(k in name.value for k in ('FLIP_MODE_VIEW','ACADDM_CHILD')):
                rect=W.RECT();self.u.GetWindowRect(child,ctypes.byref(rect));candidates.append(((rect.right-rect.left)*(rect.bottom-rect.top),int(child)))
            return True
        cb=self.callback(visit);self.u.EnumChildWindows(W.HWND(hwnd),cb,0)
        if not candidates:raise ValueError('No verified CAD drawing-view child found')
        current=self.k.GetCurrentThreadId();target=self.u.GetWindowThreadProcessId(W.HWND(hwnd),None)
        fg=self.u.GetForegroundWindow();fgthread=self.u.GetWindowThreadProcessId(fg,None) if fg else 0
        attached=[]
        try:
            for thread in set((target,fgthread))-{0,current}:
                if self.u.AttachThreadInput(current,thread,True):attached.append(thread)
            self.u.SetForegroundWindow(W.HWND(hwnd))
            time.sleep(.25)
            # Several drawings can be open at once, each with its own drawing
            # view; the largest one is not necessarily the active document. Pick
            # the view that lives inside whichever document frame already holds
            # focus, and only fall back to the largest view when nothing is focused.
            focus=self.focus_handle(hwnd)
            self.view=None
            for _area,child in sorted(candidates,reverse=True):
                if focus and (child==focus or self.u.IsChild(W.HWND(focus),W.HWND(child))):
                    self.view=child;break
            if self.view is None:self.view=max(candidates)[1]
            self.u.SetFocus(W.HWND(self.view))
        finally:
            for thread in attached:self.u.AttachThreadInput(current,thread,False)
        time.sleep(.3)
        if not self.focused(pid,hwnd):raise ValueError('CAD could not acquire verified drawing-view focus')
    def key_state(self,vk,scan,flags):
        ev=self.INPUT();ev.type=1;ev.data.key=self.KEY(vk,scan,flags,0,0)
        if self.u.SendInput(1,ctypes.byref(ev),ctypes.sizeof(self.INPUT))!=1:
            raise RuntimeError('SendInput did not accept the key event')
    def send(self,vk,scan,flags):
        self.key_state(vk,scan,flags);time.sleep(.01)
        self.key_state(vk,scan,flags|2)
    def unicode(self,ch):
        if ord(ch)>65535:raise ValueError('Only BMP command characters supported')
        self.key_state(0,ord(ch),4);time.sleep(.01)
        self.key_state(0,ord(ch),6);time.sleep(.01)
    def enter(self):self.send(13,0,0);time.sleep(.08)
    def esc(self,n=1):
        for _ in range(n):self.send(0x1B,0,0);time.sleep(.12)
    def select_all(self):
        # Ctrl+A inside a Tianzheng edit box selects its current value.
        self.u.SendInput.argtypes=[W.UINT,ctypes.POINTER(self.INPUT),ctypes.c_int]
        ctrl,key=0x11,0x41
        self.key_state(ctrl,0,0);time.sleep(.03)
        self.key_state(key,0,0);time.sleep(.03)
        self.key_state(key,0,2);time.sleep(.02)
        self.key_state(ctrl,0,2);time.sleep(.06)
    def focus_control(self,hwnd):
        # Tianzheng's parameter rows are child dialogs of a modeless panel. The
        # reliable way in is a synthetic click inside the edit: it activates the
        # panel and puts the caret in the field. Forcing focus across processes
        # only works while the caller holds the foreground right, so it stays as
        # a fallback; the click is the primary path (2026-11-19 live evidence).
        hwnd=int(hwnd);rect=W.RECT()
        for _ in range(3):
            if self.focus_handle(hwnd)==hwnd:return True
            self.u.GetWindowRect(W.HWND(hwnd),ctypes.byref(rect))
            self.click((rect.left+rect.right)//2,(rect.top+rect.bottom)//2)
            time.sleep(.35)
        if self.focus_handle(hwnd)==hwnd:return True
        root=int(self.u.GetAncestor(W.HWND(hwnd),2) or 0) or hwnd
        cur=self.k.GetCurrentThreadId()
        target=self.u.GetWindowThreadProcessId(W.HWND(hwnd),None)
        fg=self.u.GetForegroundWindow()
        fgthread=self.u.GetWindowThreadProcessId(fg,None) if fg else 0
        attached=[]
        try:
            for thread in set((target,fgthread))-{0,cur}:
                if self.u.AttachThreadInput(cur,thread,True):attached.append(thread)
            self.u.SetForegroundWindow(W.HWND(root));self.u.SetFocus(W.HWND(hwnd))
        finally:
            for thread in attached:self.u.AttachThreadInput(cur,thread,False)
        time.sleep(.2)
        return self.focus_handle(hwnd)==int(hwnd)
    def click(self,x,y):
        # A Tianzheng pick ignores synthetic clicks that arrive without motion.
        self.u.SetCursorPos(int(x),int(y));time.sleep(.05)
        for _ in range(3):
            self.u.mouse_event(0x0001,0,0,0,0);time.sleep(.02)
        self.u.mouse_event(0x0002,0,0,0,0);time.sleep(.04)
        self.u.mouse_event(0x0004,0,0,0,0);time.sleep(.05)


def staged_command(c,command,inputs):
    from .cad import retry_busy,retry_document_read,CommandCompletionTimeout
    c.guard();backend=NativeInput();backend.activate_view(c.pid,c.hwnd);bridge=CheckedInput(c.pid,c.hwnd,backend)
    old=retry_document_read(lambda:c.doc.GetVariable('LOGFILEMODE'));retry_busy(lambda:c.doc.SetVariable('LOGFILEMODE',1))
    log=Path(retry_document_read(lambda:c.doc.GetVariable('LOGFILENAME')))
    def size():return log.stat().st_size if log.exists() else 0
    def wait_output(mark,seconds):
        import pythoncom
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            pythoncom.PumpWaitingMessages()
            if size()>mark:time.sleep(.2);return True
            time.sleep(.05)
        return False
    posted=False
    try:
        bridge.check();mark=size();retry_busy(lambda:c.doc.PostCommand(command+'\r'))
        posted=True
        wait_output(mark,2)
        # Some Tianzheng prompts are rendered only after the first real input.
        for item in inputs:
            mark=size();bridge.line(item)
            if not wait_output(mark,4):raise CommandCompletionTimeout('No command output after staged input')
        c.run_command('(princ)\n')
    except Exception as exc:
        if posted:
            raise CommandCompletionTimeout('Staged command interrupted after posting: '+str(exc)) from exc
        raise
    finally:
        # Avoid another potentially blocking COM call while a failed command is
        # still active. The owned diagnostic log remains enabled on failures.
        pass
    retry_busy(lambda:c.doc.SetVariable('LOGFILEMODE',old))
