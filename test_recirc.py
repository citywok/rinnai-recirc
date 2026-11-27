#!/usr/bin/env python3
"""Mock Rinnai console + deterministic scenarios, including PUMP-INDUCED flow."""
import socket, threading, sys, logging, time, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from recirc_flow import Heater, RecircController, StreamDemand

logging.basicConfig(level=logging.ERROR, format="    log %(levelname)s %(message)s")


class MockHeater:
    def __init__(self):
        self.recirc=False; self.window=0; self.duration=5; self.enable_count=0
        self.garble=False
        self._srv=socket.socket(); self._srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        self._srv.bind(("127.0.0.1",0)); self._srv.listen(8)
        self.port=self._srv.getsockname()[1]
        threading.Thread(target=self._serve,daemon=True).start()
    def _serve(self):
        while True:
            try: conn,_=self._srv.accept()
            except OSError: return
            threading.Thread(target=self._handle,args=(conn,),daemon=True).start()
    def _handle(self,conn):
        conn.sendall(b"# monitor\r\n#   help for help\r\n")
        buf=b""
        while True:
            try: d=conn.recv(4096)
            except OSError: return
            if not d: return
            buf+=d
            while b"\n" in buf:
                line,buf=buf.split(b"\n",1)
                cmd=line.decode(errors="replace").strip()
                if cmd:
                    try: conn.sendall(self._reply(cmd).encode())
                    except OSError: return
    def _reply(self,cmd):
        if self.garble and (cmd=="heater" or cmd.startswith("find recirculation_enabled")):
            return "#   static\r\n"
        if cmd=="heater":
            w=max(self.window,0)
            return ("#   heater: op Y  prio Y  lock N  recirc %s  set_temp 120F  recirc_temp -1C\r\n"
                    "#   recirc:  %d:%02d window\r\n# schedule: Mon 13:34, enabled, out\r\n"
                    % ("Y" if self.recirc else "N", w//60, w%60))
        if cmd.startswith("set recirculation_duration"):
            self.duration=int(cmd.split()[-1]); return "#? ok\r\n"
        if cmd=="set set_recirculation_enabled true":
            self.enable_count+=1; self.recirc=True; self.window=self.duration*60
            return "#? set 'set_recirculation_enabled' to true\r\n"
        if cmd=="set set_recirculation_enabled false":
            self.recirc,self.window=False,0; return "#? ok\r\n"
        if cmd.startswith("find recirculation_enabled"):
            return "recirculation_enabled:  %s {1s}\r\n" % ("true" if self.recirc else "false")
        return "#?\r\n"
    def advance(self,dt):
        if self.recirc:
            self.window-=dt
            if self.window<=0: self.recirc,self.window=False,0

class SimFlow:
    def __init__(self,mock): self.mock=mock; self.user=False
    @property
    def active(self): return self.user or self.mock.recirc   # the trap

class Harness:
    def __init__(self,step=2.0,**kw):
        self.step=step; self.mock=MockHeater(); self.flow=SimFlow(self.mock)
        self.client=Heater("127.0.0.1",self.mock.port)
        self.ctl=RecircController(self.client,self.flow,
            recirc_minutes=kw.get("recirc_minutes",5), min_flow_s=kw.get("min_flow_s",1.0),
            cooldown_s=kw.get("cooldown_s",180.0), state_poll_s=kw.get("state_poll_s",10.0),
            max_per_hour=kw.get("max_per_hour",12),
            renew_below_s=kw.get("renew_below_s",0))
        self.t=1000.0
    def run(self,seconds,step=None):
        step=step or self.step; n=int(round(seconds/step))
        for _ in range(n):
            self.t+=step; self.mock.advance(step); self.ctl.step(self.t,self.flow.active)
        # the controller fires commands fire-and-forget, so the mock's server
        # thread needs a moment to process the write before we assert on it
        time.sleep(0.15)
    @property
    def enables(self): return self.mock.enable_count


def main():
    # make console reads fast so simulated time doesn't cost wall-clock time
    _orig = Heater._read_until_idle
    Heater._read_until_idle = staticmethod(
        lambda sock, first, idle, cap=16384: _orig(sock, min(first, .06), min(idle, .02), cap))

    res=[]
    def check(name,cond,detail=""):
        res.append(cond); print(("  PASS  " if cond else "  FAIL  ")+name+(("   ["+detail+"]") if detail else ""))

    print("\nA: one tap -> one trigger; pump's own flow must not re-trigger")
    h=Harness(); h.run(10); check("quiet: no trigger", h.enables==0, "e=%d"%h.enables)
    h.flow.user=True; h.run(4); check("tap triggers once", h.enables==1, "e=%d"%h.enables)
    check("pump running", h.mock.recirc is True); check("sensor sees pump flow", h.flow.active is True)
    h.run(240); check("no re-trigger while pump runs", h.enables==1, "e=%d"%h.enables)

    print("\nB: user keeps drawing through window end + cooldown")
    h.run(400); check("still one trigger", h.enables==1, "e=%d"%h.enables)
    check("pump stopped", h.mock.recirc is False)

    print("\nC: quiet, then a new tap -> second trigger")
    h.flow.user=False; h.run(12); h.flow.user=True; h.run(4)
    check("second trigger after quiet", h.enables==2, "e=%d"%h.enables)

    print("\nD: pump-only flow (user off) never starts another window")
    h2=Harness(step=5.0); h2.flow.user=True; h2.run(15); check("first trigger", h2.enables==1, "e=%d"%h2.enables)
    h2.flow.user=False; h2.run(900); check("no retrigger from self-flow", h2.enables==1, "e=%d"%h2.enables)

    print("\nE: unit's own schedule window at startup suppresses triggers")
    h3=Harness(); h3.mock.recirc, h3.mock.window = True, 240; h3.flow.user=True; h3.run(30)
    check("no trigger during foreign window", h3.enables==0, "e=%d"%h3.enables)
    h3.mock.recirc, h3.mock.window = False, 0; h3.run(200)
    check("cooldown after foreign window", h3.enables==0, "e=%d"%h3.enables)
    h3.flow.user=False; h3.run(12); h3.flow.user=True; h3.run(4)
    check("triggers after cooldown + quiet", h3.enables==1, "e=%d"%h3.enables)

    print("\nF: sub-second blip rejected")
    h4=Harness(step=0.25); h4.flow.user=True; h4.run(0.5); h4.flow.user=False; h4.run(5)
    check("0.5s blip ignored", h4.enables==0, "e=%d"%h4.enables)

    print("\nG: hourly rate limit")
    h5=Harness(step=1.0,max_per_hour=2,cooldown_s=1.0)
    for _ in range(5):
        h5.flow.user=True; h5.run(4); h5.flow.user=False
        h5.mock.recirc, h5.mock.window = False, 0      # pump window elapses
        h5.run(6)
    check("capped at 2/hour", h5.enables==2, "e=%d"%h5.enables)

    print("\nI: window renewal tops up a nearly-expired phase")
    h7=Harness(renew_below_s=120.0)
    h7.mock.recirc, h7.mock.window = True, 30          # running, only 30s left
    h7.ctl._phase_until = h7.t + 1800                  # our 30 min phase is active
    h7.run(10)
    check("renewal re-issued the enable", h7.enables == 1, "e=%d"%h7.enables)
    check("window topped up", h7.mock.window > 200, "w=%ds"%h7.mock.window)

    print("\nH: heater unreachable -> no crash, no trigger")
    class DeadFlow: active=False
    h6=RecircController(Heater("127.0.0.1",1,timeout=0.1), DeadFlow(), cooldown_s=180, state_poll_s=2.0)
    try:
        for i in range(10): h6.step(1000+i*0.5, False)
        check("survived unreachable heater", True)
    except Exception as e:
        check("survived unreachable heater", False, repr(e))

    print("\nJ: garbled pump-state replies are unknown, not stopped")
    h8 = Harness(state_poll_s=2.0)
    h8.flow.user = True; h8.run(4)
    check("baseline trigger", h8.enables == 1, "e=%d" % h8.enables)
    h8.mock.garble = True
    h8.run(400)                                # pump window ends mid-garble
    check("garbled reads never open the gate", h8.enables == 1, "e=%d" % h8.enables)
    h8.mock.garble = False; h8.flow.user = False
    h8.run(200)   # ride out the cooldown: it anchors to the last poll where
                  # the controller still believed the pump was running
    h8.flow.user = True; h8.run(4)
    check("clean read recovers triggering", h8.enables == 2, "e=%d" % h8.enables)

    print("\nK: a dropped push connection must not keep stale demand")
    s = StreamDemand("127.0.0.1", 1)
    s.values = {"domestic_combustion": "true"}; s.pending = "prop"; s.buf = "partial"
    s.sock = socket.socket(); s.sock.close()   # dead socket -> recv raises
    s.poll()
    check("disconnect clears cached stream state",
          s.values == {} and s.pending is None and s.buf == "",
          "values=%r" % (s.values,))

    print("\n==== %d checks: %d passed, %d failed" % (len(res), sum(res), len(res)-sum(res)))
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main())
