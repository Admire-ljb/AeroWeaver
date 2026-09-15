"""
mavsdk_adapter.py — MAVSDK + AirSim hybrid adapter

坐标系约定（全局统一）：
  所有对外接口使用「相对 spawn 点的 NED 坐标」：
    north (+) = 正北，east (+) = 正东，down (+) = 向下 / 负 = 向上
    altitude  = -down（正数米，比 spawn 高多少）

  内部转换：
    abs_n = spawn_n + rel_north      (PX4 NED absolute)
    abs_e = spawn_e + rel_east
    abs_d = spawn_d + rel_down       (spawn_d 在 AirSimNH 通常 ≈ 0)

Flight: MAVSDK -> PX4 (offboard velocity control)
Perception: AirSim RPC (camera/LiDAR/depth)
Launch: SIM_ADAPTER=mavsdk python backend/server.py
"""
import asyncio, logging, math, time, threading
from typing import Optional
from adapters.sim_adapter import (
    SimAdapter, Position, GPSPosition, VehicleState, ActionResult,
)

logger = logging.getLogger(__name__)

# 安全限制（相对 spawn，AirSimNH 地面起飞）
_MIN_ALT    = 2.0    # 最低允许高度（m），比 spawn 高至少 2m
_MAX_ALT    = 200.0  # 最高允许高度（m）
_ARRIVE_DIST = 2.5   # 到达判定半径（3D，m）


class MavsdkAdapter(SimAdapter):
    name = "mavsdk"
    description = "MAVSDK (PX4 offboard) + AirSim perception"
    supported_vehicles = ["multirotor"]

    def __init__(self, vehicle_name="PX4"):
        self._vn = vehicle_name
        self._system = None
        self._loop = None
        self._loop_thread = None
        self._asc = None           # airsim RPC client
        self._as_ok = False

        self._connected = False
        self._armed = False
        self._in_air = False

        # PX4 telemetry (absolute NED from PX4 home)
        self._abs_pos = Position()   # absolute NED
        self._vel     = [0., 0., 0.]
        self._hdg     = 0.
        self._bat_v   = 0.
        self._bat_pct = 0.
        self._mode    = "UNKNOWN"
        self._gps     = GPSPosition()

        # spawn offset (absolute NED at connect time)
        self._sp_n = 0.
        self._sp_e = 0.
        self._sp_d = 0.   # AirSimNH spawn down ≈ 0

        self.is_flying   = False
        self._stop_req   = False
        self._landed     = False
        self._safety_on  = False
        self._col_flag   = False
        self._obs_flag   = False
        self._last_obstacle_info = {}

    # ── event loop helpers ──────────────────────────────────────

    def _ensure_loop(self):
        if self._loop and self._loop.is_running():
            return
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
            maddr = "udpin://0.0.0.0:14030"
            ah, ap = "127.0.0.1", 41451

            if connection_str:
                pp = connection_str.split(":")
                if len(pp) == 2:
                    ah = pp[0]
                    try: ap = int(pp[1])
                    except: pass

            logger.info(f"MAVSDK -> {maddr}")
            if not self._ra(self._conn_mav(maddr, timeout), timeout + 5):
                return False
            self._ra(self._start_telem(), 10)
            time.sleep(1.5)   # wait for first telemetry

            # record spawn as absolute NED from PX4 home
            self._sp_n = self._abs_pos.north
            self._sp_e = self._abs_pos.east
            self._sp_d = self._abs_pos.down   # ≈ 0 in AirSimNH

            logger.info(f"AirSim -> {ah}:{ap}")
            self._conn_as(ah, ap)

            self._connected = True
            logger.info(
                f"MavsdkAdapter OK  spawn_abs=({self._sp_n:.1f},{self._sp_e:.1f},{self._sp_d:.1f})")
            return True
        except Exception as e:
            logger.error(f"connect fail: {e}")
            return False

    async def _conn_mav(self, addr, timeout):
        import os
        from mavsdk import System
        srv_host = os.getenv("MAVSDK_SERVER_HOST", "")
        srv_port = int(os.getenv("MAVSDK_SERVER_PORT", "0"))
        if srv_host and srv_port:
            self._system = System(mavsdk_server_address=srv_host, port=srv_port)
        else:
            self._system = System()
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
        for fn in [self._t_pv, self._t_gps, self._t_hdg,
                   self._t_bat, self._t_mode, self._t_arm, self._t_air]:
            asyncio.ensure_future(fn(s))

    async def _t_pv(self, s):
        async for pv in s.telemetry.position_velocity_ned():
            p, v = pv.position, pv.velocity
            self._abs_pos = Position(north=p.north_m, east=p.east_m, down=p.down_m)
            self._vel = [v.north_m_s, v.east_m_s, v.down_m_s]
    async def _t_gps(self, s):
        async for g in s.telemetry.position():
            self._gps = GPSPosition(lat=g.latitude_deg, lon=g.longitude_deg,
                                    alt=g.relative_altitude_m)
    async def _t_hdg(self, s):
        async for h in s.telemetry.heading():
            self._hdg = h.heading_deg
    async def _t_bat(self, s):
        async for b in s.telemetry.battery():
            self._bat_v, self._bat_pct = b.voltage_v, b.remaining_percent
    async def _t_mode(self, s):
        async for m in s.telemetry.flight_mode():
            self._mode = str(m)
    async def _t_arm(self, s):
        async for a in s.telemetry.armed():
            self._armed = a
    async def _t_air(self, s):
        async for ia in s.telemetry.in_air():
            self._in_air = ia

    def _conn_as(self, host, port):
        try:
            from adapters.airsim_rpc import AirSimDirectClient
            c = AirSimDirectClient(host, port, timeout=10)
            if c.connect() and c.ping():
                self._asc, self._as_ok = c, True
                logger.info("AirSim perception OK")
            else:
                self._as_ok = False
        except Exception as e:
            logger.warning(f"AirSim unavailable: {e}")
            self._as_ok = False

    def disconnect(self):
        if self.is_flying:
            self._stop_req = True
            time.sleep(1)
        self._safety_on = False
        self._connected = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ── coordinate helpers ──────────────────────────────────────

    def _rel_to_abs(self, rel_north, rel_east, rel_down):
        """相对 spawn NED → PX4 绝对 NED"""
        return (self._sp_n + rel_north,
                self._sp_e + rel_east,
                self._sp_d + rel_down)

    def _abs_to_rel(self, abs_north, abs_east, abs_down):
        """PX4 绝对 NED → 相对 spawn NED"""
        return (abs_north - self._sp_n,
                abs_east  - self._sp_e,
                abs_down  - self._sp_d)

    def _clamp_altitude(self, rel_down) -> float:
        """限制高度在安全范围内（相对 spawn）。rel_down 负值 = 高于 spawn。"""
        alt = -rel_down   # 正数 = 高于 spawn
        if alt < _MIN_ALT:
            logger.warning(f"⚠️ 目标高度 {alt:.1f}m < 最低 {_MIN_ALT:.1f}m，自动修正")
            alt = _MIN_ALT
        elif alt > _MAX_ALT:
            logger.warning(f"⚠️ 目标高度 {alt:.1f}m > 最高 {_MAX_ALT:.1f}m，自动修正")
            alt = _MAX_ALT
        return -alt

    # ── state ───────────────────────────────────────────────────

    def is_connected(self): return self._connected

    def get_position(self) -> Position:
        """返回相对 spawn 的 NED 位置。"""
        rn, re, rd = self._abs_to_rel(
            self._abs_pos.north, self._abs_pos.east, self._abs_pos.down)
        return Position(north=rn, east=re, down=rd)

    def get_gps(self): return self._gps
    def get_battery(self): return (self._bat_v, self._bat_pct)
    def is_armed(self): return self._armed
    def is_in_air(self): return self._in_air

    def get_state(self) -> VehicleState:
        return VehicleState(
            armed=self._armed,
            in_air=self._in_air,
            mode=self._mode,
            position_ned=self.get_position(),
            position_gps=self._gps,
            battery_voltage=self._bat_v,
            battery_percent=self._bat_pct,
            heading_deg=self._hdg,
            velocity=list(self._vel),
        )

    # ── offboard helpers ────────────────────────────────────────

    async def _enter_offboard(self):
        from mavsdk.offboard import VelocityNedYaw
        await self._system.offboard.set_velocity_ned(
            VelocityNedYaw(0, 0, 0, self._hdg))
        await asyncio.sleep(0.2)
        try:
            await self._system.offboard.start()
        except Exception as e:
            if "already" not in str(e).lower(): raise

    async def _exit_offboard(self):
        try: await self._system.offboard.stop()
        except: pass

    async def _vel_cmd(self, vn, ve, vd, yaw):
        from mavsdk.offboard import VelocityNedYaw
        await self._system.offboard.set_velocity_ned(
            VelocityNedYaw(vn, ve, vd, yaw))

    # ── safety thread ───────────────────────────────────────────

    def _start_safety(self):
        if not self._as_ok: return
        self._safety_on = True
        self._col_flag = self._obs_flag = False
        threading.Thread(target=self._safety_loop, daemon=True).start()

    def _safety_loop(self):
        while self._safety_on:
            try:
                c = self._asc._rpc.call("simGetCollisionInfo", self._vn)
                if c and c.get("has_collided"):
                    self._col_flag = True
            except: pass
            try: self._chk_depth()
            except: pass
            time.sleep(0.5)

    def _chk_depth(self):
        import numpy as np
        rs = self._asc.sim_get_images([{
            'camera_name': 'cam_front', 'image_type': 2,
            'pixels_as_float': True, 'compress': False,
        }], vehicle_name=self._vn)
        if not rs: return
        r = rs[0]; h, w = r.get('height', 0), r.get('width', 0)
        data = r.get('image_data_float', [])
        if h == 0 or w == 0 or len(data) < h * w: return
        arr = np.array(data[:h*w], dtype=np.float32).reshape(h, w)
        ctr = arr[h//3:2*h//3, w//4:3*w//4]
        v = ctr[(ctr > 0) & (ctr < 100)]
        if len(v) == 0: return
        d = float(np.percentile(v, 5))
        if d < 8.0:
            self._obs_flag = True
            self._last_obstacle_info = {'front_dist': d, 'direction': '前方'}

    # ── flight ops ──────────────────────────────────────────────

    def arm(self):
        try:
            self._ra(self._system.action.arm(), 10)
            return ActionResult(True, "Armed")
        except Exception as e:
            return ActionResult(False, str(e))

    def disarm(self):
        try:
            self._ra(self._system.action.disarm(), 10)
            return ActionResult(True, "Disarmed")
        except Exception as e:
            return ActionResult(False, str(e))

    def takeoff(self, altitude=5.) -> ActionResult:
        """起飞到相对 spawn 的高度（米）。"""
        if not self._connected:
            return ActionResult(False, "Not connected")
        altitude = max(altitude, _MIN_ALT)
        try:
            self._landed = False
            self._ra(self._system.action.set_takeoff_altitude(altitude), 5)
            if not self._armed:
                self._ra(self._system.action.arm(), 10)
            self._ra(self._system.action.takeoff(), 30)
            dl = time.time() + 30
            while time.time() < dl:
                cur_alt = -(self._abs_pos.down - self._sp_d)
                if abs(cur_alt - altitude) < 1.5:
                    break
                time.sleep(0.3)
            final_alt = -(self._abs_pos.down - self._sp_d)
            logger.info(f"Takeoff done: {final_alt:.1f}m")
            return ActionResult(True, f"Takeoff OK: {final_alt:.1f}m")
        except Exception as e:
            return ActionResult(False, str(e))

    def land(self) -> ActionResult:
        if not self._connected:
            return ActionResult(False, "Not connected")
        try:
            self._safety_on = False
            try: self._ra(self._exit_offboard())
            except: pass
            self._ra(self._system.action.land(), 60)
            dl = time.time() + 60
            while time.time() < dl:
                if not self._in_air: break
                time.sleep(0.5)
            self._landed = True
            return ActionResult(True, "Landed")
        except Exception as e:
            return ActionResult(False, str(e))

    def hover(self, duration=5.) -> ActionResult:
        if not self._connected:
            return ActionResult(False, "Not connected")
        try:
            self._ra(self._vel_cmd(0, 0, 0, self._hdg))
            time.sleep(duration)
            return ActionResult(True, f"Hovered {duration}s")
        except Exception as e:
            return ActionResult(False, str(e))

    def return_to_launch(self) -> ActionResult:
        if not self._connected:
            return ActionResult(False, "Not connected")
        try:
            self._safety_on = False
            try: self._ra(self._exit_offboard())
            except: pass
            self._ra(self._system.action.return_to_launch(), 120)
            dl = time.time() + 120
            while time.time() < dl:
                if not self._in_air: break
                time.sleep(1)
            return ActionResult(True, "RTL done")
        except Exception as e:
            return ActionResult(False, str(e))

    def fly_to_ned(self, north: float, east: float, down: float,
                   speed: float = 5.) -> ActionResult:
        """
        飞到相对 spawn 的 NED 坐标。
        north/east: 米，相对 spawn 点偏移
        down: 米，负值 = 比 spawn 高（例如 -20 = 高于 spawn 20m）
        speed: m/s
        """
        if not self._connected:
            return ActionResult(False, "Not connected")
        try:
            # 高度安全限制
            down = self._clamp_altitude(down)

            # 转换为 PX4 绝对 NED
            tgt_n, tgt_e, tgt_d = self._rel_to_abs(north, east, down)

            logger.info(
                f"fly_to_ned: rel({north:.1f},{east:.1f},{down:.1f}) "
                f"-> abs({tgt_n:.1f},{tgt_e:.1f},{tgt_d:.1f})")

            speed = min(max(speed, 0.5), 10.0)
            self._stop_req = False
            self.is_flying = True
            self._start_safety()
            self._ra(self._enter_offboard())

            TIMEOUT = 120.
            start = time.time()
            try:
                while time.time() - start < TIMEOUT:
                    # stop request
                    if self._stop_req:
                        self._stop_req = False
                        self._ra(self._vel_cmd(0, 0, 0, self._hdg))
                        return ActionResult(False, "Flight stopped by user")

                    # collision
                    if self._col_flag:
                        self._col_flag = False
                        self._ra(self._vel_cmd(0, 0, 0, self._hdg))
                        return ActionResult(False, "Collision detected, hovering")

                    # obstacle
                    if self._obs_flag:
                        self._obs_flag = False
                        self._ra(self._vel_cmd(0, 0, 0, self._hdg))
                        info = self._last_obstacle_info
                        return ActionResult(False,
                            f"Obstacle {info.get('front_dist', 0):.1f}m "
                            f"{info.get('direction', 'ahead')}")

                    # current absolute NED
                    cn = self._abs_pos.north
                    ce = self._abs_pos.east
                    cd = self._abs_pos.down

                    dn, de, dd = tgt_n - cn, tgt_e - ce, tgt_d - cd
                    h_dist   = math.sqrt(dn*dn + de*de)
                    dist_3d  = math.sqrt(dn*dn + de*de + dd*dd)

                    # arrived?
                    if dist_3d < _ARRIVE_DIST:
                        self._ra(self._vel_cmd(0, 0, 0, self._hdg))
                        rel_pos = self.get_position()
                        logger.info(
                            f"fly_to_ned: arrived err={dist_3d:.2f}m "
                            f"pos={rel_pos}")
                        return ActionResult(True, f"Arrived (err={dist_3d:.2f}m)")

                    # speed profile: decelerate when close
                    SLOW_DIST = 15.
                    if h_dist > SLOW_DIST:
                        v_h = speed
                    else:
                        v_h = max(speed * (h_dist / SLOW_DIST), 0.4)

                    # horizontal velocity (world NED)
                    if h_dist > 0.3:
                        vn = v_h * (dn / h_dist)
                        ve = v_h * (de / h_dist)
                    else:
                        vn, ve = 0., 0.

                    # vertical velocity (proportional, clamped ±3 m/s)
                    vd = max(min(dd * 0.5, 3.0), -3.0)

                    # yaw towards target
                    if h_dist > 2.:
                        yaw = math.degrees(math.atan2(de, dn))
                    else:
                        yaw = self._hdg

                    logger.debug(
                        f"vel: h={h_dist:.1f}m 3d={dist_3d:.1f}m "
                        f"v=({vn:.2f},{ve:.2f},{vd:.2f}) yaw={yaw:.0f}")

                    self._ra(self._vel_cmd(vn, ve, vd, yaw))
                    time.sleep(0.1)   # 10 Hz setpoints

                self._ra(self._vel_cmd(0, 0, 0, self._hdg))
                return ActionResult(False, "fly_to_ned timeout")

            finally:
                self.is_flying = False
                self._safety_on = False

        except Exception as e:
            self.is_flying = False
            return ActionResult(False, str(e))

    def change_altitude_relative(self, delta: float, speed=3.) -> ActionResult:
        """
        改变高度（正数 = 升高，负数 = 降低），单位米，相对当前高度。
        """
        if not self._connected:
            return ActionResult(False, "Not connected")
        try:
            cur_rel_down = self._abs_pos.down - self._sp_d   # 相对 spawn
            cur_alt = -cur_rel_down
            tgt_alt = cur_alt + delta
            tgt_rel_down = self._clamp_altitude(-tgt_alt)    # 经过安全限制
            tgt_d = self._sp_d + tgt_rel_down                # PX4 绝对

            final_tgt_alt = -tgt_rel_down
            logger.info(
                f"change_alt: {cur_alt:.1f}m -> {final_tgt_alt:.1f}m (Δ={delta:+.1f})")

            self._stop_req = False
            self.is_flying = True
            self._ra(self._enter_offboard())

            TIMEOUT = 60.
            start = time.time()
            try:
                while time.time() - start < TIMEOUT:
                    if self._stop_req:
                        self._stop_req = False
                        self._ra(self._vel_cmd(0, 0, 0, self._hdg))
                        return ActionResult(False, "Altitude change stopped")

                    cd = self._abs_pos.down
                    err = abs(cd - tgt_d)
                    if err < 1.0:
                        self._ra(self._vel_cmd(0, 0, 0, self._hdg))
                        final = -(self._abs_pos.down - self._sp_d)
                        logger.info(f"change_alt done: {final:.1f}m")
                        return ActionResult(True,
                            f"Altitude: {cur_alt:.1f}m -> {final:.1f}m")

                    dd = tgt_d - cd
                    vd = max(min(dd * 0.5, 3.), -3.)
                    self._ra(self._vel_cmd(0, 0, vd, self._hdg))
                    time.sleep(0.1)

                self._ra(self._vel_cmd(0, 0, 0, self._hdg))
                return ActionResult(False, "Altitude change timeout")
            finally:
                self.is_flying = False

        except Exception as e:
            self.is_flying = False
            return ActionResult(False, str(e))

    def request_stop(self):
        self._stop_req = True

    def set_velocity_body(self, forward, right, down,
                          duration=1., yaw_rate=0.) -> ActionResult:
        """Body-frame velocity（手动/驾驶舱控制）。"""
        if not self._connected:
            return ActionResult(False, "Not connected")
        try:
            self._ra(self._enter_offboard())
            from mavsdk.offboard import VelocityBodyYawspeed
            async def _send():
                await self._system.offboard.set_velocity_body(
                    VelocityBodyYawspeed(forward, right, down, yaw_rate))
            self._ra(_send())
            time.sleep(duration)
            return ActionResult(True, "velocity_body sent")
        except Exception as e:
            return ActionResult(False, str(e))

    def stop_velocity(self) -> ActionResult:
        try:
            self._ra(self._vel_cmd(0, 0, 0, self._hdg))
            return ActionResult(True, "Stopped")
        except Exception as e:
            return ActionResult(False, str(e))

    # ── AirSim perception passthrough ───────────────────────────

    def get_image_base64(self, camera_name='front_center'):
        if not self._as_ok: return None
        try:
            import base64, cv2, numpy as np
            rs = self._asc.sim_get_images([{
                "camera_name": camera_name, "image_type": 0,
                "pixels_as_float": False, "compress": False,
            }], vehicle_name=self._vn)
            if not rs: return None
            r = rs[0]; h, w = r.get("height", 0), r.get("width", 0)
            data = r.get("image_data_uint8") or r.get("image_data", b"")
            if isinstance(data, str): data = base64.b64decode(data)
            if h > 0 and w > 0 and len(data) >= h * w * 3:
                img = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 3)
                _, buf = cv2.imencode(".jpg", img,
                                      [cv2.IMWRITE_JPEG_QUALITY, 80])
                return base64.b64encode(buf.tobytes()).decode("ascii")
        except Exception as e:
            logger.warning(f"get_image_base64 error: {e}")
        return None
