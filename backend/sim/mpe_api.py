"""Bounded native-MPE demo sessions, separate from the active flight runtime."""

import threading
import time
import uuid
import os
from flask import Blueprint, jsonify, request

from sim.mpe_catalog import PAPER_SCENARIOS


def create_mpe_blueprint():
    api = Blueprint("mpe_lab", __name__, url_prefix="/api/mpe")
    sessions = {}
    lock = threading.RLock()

    def lookup(key):
        if key not in sessions:
            raise KeyError("Unknown or expired MPE session")
        return sessions[key]

    @api.errorhandler(KeyError)
    def missing(exc):
        return jsonify(error=str(exc)), 404

    @api.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @api.get("/catalog")
    def catalog():
        from skills.mpe_skills import TASK_SKILLS
        return jsonify(scenarios=[{"id": s, "roles": TASK_SKILLS[s]} for s in PAPER_SCENARIOS],
                       policies=["local_heuristic", "random_skills"], policy_type="engineering_mock_no_llm")

    @api.post("/sessions")
    def create():
        # The web client renders state on its canvas; the server needs no SDL display.
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
        from sim.mpe_runtime import MPERuntime
        data = request.get_json(silent=True) or {}
        policy = data.get("policy", "local_heuristic")
        if policy not in {"local_heuristic", "random_skills"}:
            raise ValueError("Select an available engineering policy")
        with lock:
            # Dispose finished sessions after 30 minutes; live sessions are never replaced.
            for key, session in list(sessions.items()):
                if not session["running"] and time.monotonic() - session["touched"] > 1800:
                    session["runtime"].close()
                    del sessions[key]
            if len(sessions) >= 4:
                return jsonify(error="Close an existing MPE session first"), 409
            runtime = MPERuntime(data.get("scenario", "simple_spread"), int(data.get("seed", 0)), int(data.get("max_cycles", 100)))
            key = uuid.uuid4().hex
            sessions[key] = {"runtime": runtime, "policy": policy, "running": False, "generation": 0,
                             "error": "", "touched": time.monotonic()}
            return jsonify(id=key, **runtime.snapshot(), catalog={a: s.describe() for a, s in runtime.skills.items()})

    @api.get("/sessions/<key>")
    def snapshot(key):
        with lock:
            session = lookup(key)
            session["touched"] = time.monotonic()
            return jsonify(**session["runtime"].snapshot(), running=session["running"], error=session["error"], policy=session["policy"])

    @api.post("/sessions/<key>/<command>")
    def control(key, command):
        if command not in {"run", "pause", "step"}:
            raise ValueError("Unknown MPE command")
        with lock:
            session = lookup(key)
            if command == "pause":
                session["running"] = False
                session["generation"] += 1
            elif command == "step":
                if session["running"]:
                    return jsonify(error="Pause before single stepping"), 409
                session["runtime"].policy_step(session["policy"])
            elif not session["running"] and not session["runtime"].done:
                session["generation"] += 1
                generation = session["generation"]
                session["running"] = True
                def worker():
                    deadline = time.monotonic()
                    while True:
                        with lock:
                            if key not in sessions or not session["running"] or generation != session["generation"]:
                                return
                            try:
                                session["runtime"].policy_step(session["policy"])
                                if session["runtime"].done:
                                    session["running"] = False
                                    return
                            except Exception as exc:
                                session["error"] = f"{type(exc).__name__}: {exc}"
                                session["running"] = False
                                return
                        deadline += 0.1
                        time.sleep(max(0, deadline - time.monotonic()))
                threading.Thread(target=worker, name="mpe-lab", daemon=True).start()
            return jsonify(ok=True, running=session["running"])

    @api.get("/sessions/<key>/trace")
    def trace(key):
        with lock:
            session = lookup(key)
            return jsonify(scenario=session["runtime"].scenario, policy=session["policy"],
                           policy_type="engineering_mock_no_llm", reward_source="unmodified_mpe2",
                           seed=session["runtime"].seed, trace=session["runtime"].trace)

    @api.delete("/sessions/<key>")
    def close(key):
        with lock:
            session = lookup(key)
            session["running"] = False
            session["runtime"].close()
            del sessions[key]
            return jsonify(ok=True)

    return api
