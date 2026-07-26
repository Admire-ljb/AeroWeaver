"""
px4_adapter.py — Pure PX4 SITL adapter via MAVSDK

Flight control only, no AirSim perception.
For PX4 + Gazebo simulation without AirSim.

Launch: SIM_ADAPTER=px4 python server.py
"""
import asyncio, logging, math, os, time, threading
from adapters.sim_adapter import (
    SimAdapter, Position, GPSPosition, VehicleState, ActionResult,
)

logger = logging.getLogger(__name__)
_MIN_ALT, _MAX_ALT, _ARRIVE_DIST = 2.0, 200.0, 2.5


class PX4Adapter(SimAdapter):
    name = "px4"
    description = "PX4 SITL via MAVSDK (Gazebo, no AirSim)"
    supported_vehicles = ["multirotor"]

    def __init__(self):
        self._system = self._loop = self._loop_thread = None
        self._connected = self._armed = self._in_air = False
        self._abs_pos, self._vel, self._hdg = Position(), [0.,0.,0.], 0.
        self._bat_v = self._bat_pct = 0.
        self._mode, self._gps = "UNKNOWN", GPSPosition()
        self._sp_n = self._sp_e = self._sp_d = 0.
        self.is_flying = self._stop_req = self._landed = False

    def _ensure_loop(self):
        if self._loop and self._loop.is_running(): return
        self._loop = asyncio.new_event_loop()
        def _r():
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()
        self._loop_thread = threading.Thread(target=_r, daemon=True)
        self._loop_thread.start()

    def _ra(self, coro, timeout=60.):
        self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    # ── connect ─────────────────────────────────────────────────
    def connect(self, connection_str="", timeout=15.) -> bool:
        try:
            self._ensure_loop()
            maddr = connection_str or "udp://:14540"
            logger.info(f"PX4Adapter -> {maddr}")
            if not self._ra(self._conn_mav(maddr, timeout), timeout + 5):
                return False
            self._ra(self._start_telem(), 10)
            time.sleep(1.5)
            self._sp_n, self._sp_e, self._sp_d = (
                self._abs_pos.north, self._abs_pos.east, self._abs_pos.down)
            self._connected = True
            logger.info(f"PX4Adapter OK spawn=({self._sp_n:.1f},{self._sp_e:.1f},{self._sp_d:.1f})")
            return True
        except Exception as e:
            logger.error(f"PX4Adapter connect fail: {e}")
            return False

    async def _conn_mav(self, addr, timeout):
        from mavsdk import System
        sh, sp = os.getenv("MAVSDK_SERVER_HOST",""), int(os.getenv("MAVSDK_SERVER_PORT","0"))
        self._system = System(mavsdk_server_address=sh, port=sp) if sh and sp else System()
        await self._system.connect(system_address=addr)
        dl = asyncio.get_event_loop().time() + timeout
        async for st in self._system.core.connection_state():
            if st.is_connected: return True
            if asyncio.get_event_loop().time() > dl: return False
        return False

    async def _start_telem(self):
        s = self._system
        try: await s.telemetry.set_rate_position_velocity_ned(10.)
        except: pass
        for fn in [self._t_pv,self._t_gps,self._t_hdg,self._t_bat,self._t_mode,self._t_arm,self._t_air]:
            asyncio.ensure_future(fn(s))

    async def _t_pv(self,s):
        async for pv in s.telemetry.position_velocity_ned():
            p,v = pv.position, pv.velocity
            self._abs_pos = Position(north=p.north_m, east=p.east_m, down=p.down_m)
            self._vel = [v.north_m_s, v.east_m_s, v.down_m_s]
    async def _t_gps(self,s):
        async for g in s.telemetry.position():
            self._gps = GPSPosition(lat=g.latitude_deg, lon=g.longitude_deg, alt=g.relative_altitude_m)
    async def _t_hdg(self,s):
        async for h in s.telemetry.heading(): self._hdg = h.heading_deg
    async def _t_bat(self,s):
        async for b in s.telemetry.battery(): self._bat_v, self._bat_pct = b.voltage_v, b.remaining_percent
    async def _t_mode(self,s):
        async for m in s.telemetry.flight_mode(): self._mode = str(m)
    async def _t_arm(self,s):
        async for a in s.telemetry.armed(): self._armed = a
    async def _t_air(self,s):
        async for ia in s.telemetry.in_air(): self._in_air = ia

    def disconnect(self):
        if self.is_flying: self._stop_req = True; time.sleep(1)
        self._connected = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ── helpers ─────────────────────────────────────────────────
    def _rel_to_abs(self,rn,re,rd): return self._sp_n+rn, self._sp_e+re, self._sp_d+rd
    def _abs_to_rel(self,an,ae,ad): return an-self._sp_n, ae-self._sp_e, ad-self._sp_d
    def _clamp_alt(self, rd):
        a = -rd
        return -max(min(a, _MAX_ALT), _MIN_ALT)

    # ── state ───────────────────────────────────────────────────
    def is_connected(self): return self._connected
    def get_position(self) -> Position:
        rn,re,rd = self._abs_to_rel(self._abs_pos.north, self._abs_pos.east, self._abs_pos.down)
        return Position(north=rn, east=re, down=rd)
    def get_gps(self): return self._gps
    def get_battery(self): return (self._bat_v, self._bat_pct)
    def is_armed(self): return self._armed
    def is_in_air(self): return self._in_air
    def get_state(self) -> VehicleState:
        return VehicleState(armed=self._armed, in_air=self._in_air, mode=self._mode,
            position_ned=self.get_position(), position_gps=self._gps,
            battery_voltage=self._bat_v, battery_percent=self._bat_pct,
            heading_deg=self._hdg, velocity=list(self._vel))

    # ── offboard ────────────────────────────────────────────────
    async def _enter_offboard(self):
        from mavsdk.offboard import VelocityNedYaw
        await self._system.offboard.set_velocity_ned(VelocityNedYaw(0,0,0,self._hdg))
        await asyncio.sleep(0.2)
        try: await self._system.offboard.start()
        except Exception as e:
            if "already" not in str(e).lower(): raise
    async def _exit_offboard(self):
        try: await self._system.offboard.stop()
        except: pass
    async def _vel_cmd(self, vn, ve, vd, yaw):
        from mavsdk.offboard import VelocityNedYaw
        await self._system.offboard.set_velocity_ned(VelocityNedYaw(vn,ve,vd,yaw))

    # ── flight ops ──────────────────────────────────────────────
    def arm(self):
        try: self._ra(self._system.action.arm(), 10); return ActionResult(True, "Armed")
        except Exception as e: return ActionResult(False, str(e))
    def disarm(self):
        try: self._ra(self._system.action.disarm(), 10); return ActionResult(True, "Disarmed")
        except Exception as e: return ActionResult(False, str(e))

    def takeoff(self, altitude=5.) -> ActionResult:
        if not self._connected: return ActionResult(False, "Not connected")
        altitude = max(altitude, _MIN_ALT)
        try:
            self._landed = False
            self._ra(self._system.action.set_takeoff_altitude(altitude), 5)
            if not self._armed: self._ra(self._system.action.arm(), 10)
            self._ra(self._system.action.takeoff(), 30)
            dl = time.time() + 30
            while time.time() < dl:
                if abs(-(self._abs_pos.down - self._sp_d) - altitude) < 1.5: break
                time.sleep(0.3)
            fa = -(self._abs_pos.down - self._sp_d)
            return ActionResult(True, f"Takeoff OK: {fa:.1f}m")
        except Exception as e: return ActionResult(False, str(e))

    def land(self) -> ActionResult:
        if not self._connected: return ActionResult(False, "Not connected")
        try:
            try: self._ra(self._exit_offboard())
            except: pass
            self._ra(self._system.action.land(), 60)
            dl = time.time() + 60
            while time.time() < dl:
                if not self._in_air: break
                time.sleep(0.5)
            self._landed = True
            return ActionResult(True, "Landed")
        except Exception as e: return ActionResult(False, str(e))

    def hover(self, duration=5.) -> ActionResult:
        if not self._connected: return ActionResult(False, "Not connected")
        try:
            self._ra(self._enter_offboard())
            self._ra(self._vel_cmd(0,0,0,self._hdg))
            time.sleep(duration)
            return ActionResult(True, f"Hovered {duration}s")
        except Exception as e: return ActionResult(False, str(e))

    def return_to_launch(self) -> ActionResult:
        if not self._connected: return ActionResult(False, "Not connected")
        try:
            try: self._ra(self._exit_offboard())
            except: pass
            self._ra(self._system.action.return_to_launch(), 120)
            dl = time.time() + 120
            while time.time() < dl:
                if not self._in_air: break
                time.sleep(1)
            return ActionResult(True, "RTL done")
        except Exception as e: return ActionResult(False, str(e))

    def fly_to_ned(self, north, east, down, speed=5.) -> ActionResult:
        if not self._connected: return ActionResult(False, "Not connected")
        try:
            down = self._clamp_alt(down)
            tgt_n, tgt_e, tgt_d = self._rel_to_abs(north, east, down)
            speed = min(max(speed, 0.5), 10.0)
            self._stop_req = False
            self.is_flying = True
            self._ra(self._enter_offboard())
            start = time.time()
            try:
                while time.time() - start < 120.:
                    if self._stop_req:
                        self._stop_req = False
                        self._ra(self._vel_cmd(0,0,0,self._hdg))
                        return ActionResult(False, "Stopped")
                    cn,ce,cd = self._abs_pos.north, self._abs_pos.east, self._abs_pos.down
                    dn,de,dd = tgt_n-cn, tgt_e-ce, tgt_d-cd
                    hd = math.sqrt(dn*dn+de*de)
                    d3 = math.sqrt(dn*dn+de*de+dd*dd)
                    if d3 < _ARRIVE_DIST:
                        self._ra(self._vel_cmd(0,0,0,self._hdg))
                        return ActionResult(True, f"Arrived (err={d3:.2f}m)")
                    vh = speed if hd > 15. else max(speed*(hd/15.), 0.4)
                    vn = vh*(dn/hd) if hd > 0.3 else 0.
                    ve = vh*(de/hd) if hd > 0.3 else 0.
                    vd = max(min(dd*0.5, 3.), -3.)
                    yaw = math.degrees(math.atan2(de,dn)) if hd > 2. else self._hdg
                    self._ra(self._vel_cmd(vn,ve,vd,yaw))
                    time.sleep(0.1)
                self._ra(self._vel_cmd(0,0,0,self._hdg))
                return ActionResult(False, "fly_to_ned timeout")
            finally:
                self.is_flying = False
        except Exception as e:
            self.is_flying = False
            return ActionResult(False, str(e))

    def change_altitude_relative(self, delta, speed=3.) -> ActionResult:
        if not self._connected: return ActionResult(False, "Not connected")
        try:
            cur_rd = self._abs_pos.down - self._sp_d
            tgt_rd = self._clamp_alt(-((-cur_rd) + delta))
            tgt_d = self._sp_d + tgt_rd
            self._stop_req = False
            self.is_flying = True
            self._ra(self._enter_offboard())
            start = time.time()
            try:
                while time.time() - start < 60.:
                    if self._stop_req:
                        self._ra(self._vel_cmd(0,0,0,self._hdg))
                        return ActionResult(False, "Stopped")
                    err = abs(self._abs_pos.down - tgt_d)
                    if err < 1.0:
                        self._ra(self._vel_cmd(0,0,0,self._hdg))
                        fa = -(self._abs_pos.down - self._sp_d)
                        return ActionResult(True, f"Altitude: {fa:.1f}m")
                    dd = tgt_d - self._abs_pos.down
                    self._ra(self._vel_cmd(0, 0, max(min(dd*0.5,3.),-3.), self._hdg))
                    time.sleep(0.1)
                self._ra(self._vel_cmd(0,0,0,self._hdg))
                return ActionResult(False, "Altitude change timeout")
            finally:
                self.is_flying = False
        except Exception as e:
            self.is_flying = False
            return ActionResult(False, str(e))

    def request_stop(self):
        self._stop_req = True

    def set_velocity_body(self, forward, right, down, duration=1., yaw_rate=0.) -> ActionResult:
        if not self._connected: return ActionResult(False, "Not connected")
        try:
            self._ra(self._enter_offboard())
            from mavsdk.offboard import VelocityBodyYawspeed
            async def _send():
                await self._system.offboard.set_velocity_body(
                    VelocityBodyYawspeed(forward, right, down, yaw_rate))
            self._ra(_send())
            time.sleep(duration)
            return ActionResult(True, "velocity_body sent")
        except Exception as e: return ActionResult(False, str(e))

    def stop_velocity(self) -> ActionResult:
        try:
            self._ra(self._vel_cmd(0,0,0,self._hdg))
            return ActionResult(True, "Stopped")
        except Exception as e: return ActionResult(False, str(e))
