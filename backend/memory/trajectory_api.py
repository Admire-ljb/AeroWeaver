"""Read-only episode and transition inspection, independent of learning retrieval."""

from contextlib import closing
import json
import sqlite3

from flask import jsonify, request


class TrajectoryBrowser:
    def __init__(self, path):
        self.path = path.resolve()

    def query(self, sql, parameters=()):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, parameters)]

    @staticmethod
    def where(*, episode=None, agent=None, role=None, query="", failed=False):
        clauses, values = ["1=1"], []
        for column, value in (("mission", episode), ("agent", agent), ("json_extract(payload,'$.role')", role)):
            if value:
                clauses.append(column + "=?")
                values.append(value)
        if failed:
            clauses.append("json_extract(payload,'$.success')=0")
        if query.strip():
            # Literal substring search: UAV_1 must not match UAVx1.
            text = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            columns = ("mission", "agent", "json_extract(payload,'$.task')", "json_extract(payload,'$.role')",
                       "json_extract(payload,'$.skill')", "json_extract(payload,'$.metadata.experiment_id')")
            clauses.append("(" + " OR ".join(c + " LIKE ? ESCAPE '\\'" for c in columns) + ")")
            values.extend(["%" + text + "%"] * len(columns))
        return " AND ".join(clauses), values

    def stats(self):
        return self.query("""SELECT COUNT(*) AS records, COUNT(DISTINCT mission) AS episodes,
            COALESCE(SUM(json_extract(payload,'$.success')=0),0) AS failed_records,
            COUNT(reward) AS rewarded_records, COALESCE(SUM(reward=0),0) AS zero_reward_records,
            COALESCE(SUM(finalized=0),0) AS pending_returns FROM transitions""")[0]

    def episodes(self, *, query="", limit=20, offset=0):
        where, values = self.where(query=query)
        matching = "SELECT DISTINCT mission FROM transitions WHERE " + where
        total = self.query("SELECT COUNT(*) AS n FROM (" + matching + ")", values)[0]["n"]
        rows = self.query("""SELECT mission AS episode_id,
            MIN(json_extract(payload,'$.task')) AS task, COUNT(*) AS records,
            COUNT(DISTINCT agent) AS agents, COUNT(DISTINCT step) AS steps,
            MIN(finalized) AS returns_finalized,
            COALESCE(SUM(json_extract(payload,'$.success')=0),0) AS failed_records,
            MIN(json_extract(payload,'$.timestamp')) AS started_at,
            MAX(json_extract(payload,'$.timestamp')) AS updated_at,
            MIN(json_extract(payload,'$.metadata.condition')) AS policy,
            MIN(json_extract(payload,'$.metadata.seed')) AS seed,
            MIN(json_extract(payload,'$.metadata.experiment_id')) AS experiment_id
            FROM transitions WHERE mission IN (""" + matching + """ )
            GROUP BY mission ORDER BY updated_at DESC, mission DESC LIMIT ? OFFSET ?""", [*values, limit, offset])
        return {"items": rows, "total": total, "limit": limit, "offset": offset}

    def episode(self, episode):
        rows = self.query("""SELECT agent AS agent_id, trajectory AS trajectory_id,
            MIN(json_extract(payload,'$.role')) AS role, COUNT(*) AS records,
            MIN(step) AS first_step, MAX(step) AS last_step, SUM(reward) AS reward_sum,
            MIN(finalized) AS returns_finalized,
            SUM(json_extract(payload,'$.success')=0) AS failed_records
            FROM transitions WHERE mission=? GROUP BY agent,trajectory ORDER BY agent,trajectory""", (episode,))
        if not rows:
            return None
        first = self.query("SELECT payload FROM transitions WHERE mission=? ORDER BY rowid LIMIT 1", (episode,))[0]
        record = json.loads(first["payload"])
        return {"episode_id": episode, "task": record["task"], "agents": rows,
                "gamma": record["gamma"], "reward_source": record["reward_source"],
                "metadata": record["metadata"]}

    def transitions(self, *, limit=40, offset=0, **filters):
        where, values = self.where(**filters)
        total = self.query("SELECT COUNT(*) AS n FROM transitions WHERE " + where, values)[0]["n"]
        ordering = "step,agent,trajectory,id" if filters.get("episode") else "rowid DESC"
        rows = self.query("""SELECT id AS experience_id,mission AS episode_id,agent AS agent_id,
            trajectory AS trajectory_id,step AS step_index,reward AS immediate_reward,
            return_value,finalized AS return_finalized,
            json_extract(payload,'$.skill') AS skill, json_extract(payload,'$.role') AS role,
            json_extract(payload,'$.task') AS task, json_extract(payload,'$.success') AS success,
            json_extract(payload,'$.trace.error') AS error,
            json_extract(payload,'$.metadata.continued_skill') AS continued_skill,
            json_extract(payload,'$.trace.selector_source') AS selector_source
            FROM transitions WHERE """ + where + " ORDER BY " + ordering + " LIMIT ? OFFSET ?", [*values, limit, offset])
        return {"items": rows, "total": total, "limit": limit, "offset": offset}

    def transition(self, record_id):
        rows = self.query("SELECT * FROM transitions WHERE id=?", (record_id,))
        if not rows:
            return None
        row = rows[0]
        record = json.loads(row["payload"])
        record.update(episode_id=row["mission"], return_value=row["return_value"], return_finalized=bool(row["finalized"]))
        return record


def register_trajectory_api(app, get_memory):
    def browser():
        memory = get_memory()
        if memory is None:
            raise RuntimeError("Trajectory memory is not initialized")
        return TrajectoryBrowser(memory.path)

    def pagination():
        limit, offset = int(request.args.get("limit", 40)), int(request.args.get("offset", 0))
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("Expected limit 1..200 and offset >= 0")
        return {"limit": limit, "offset": offset}

    def respond(operation):
        try:
            value = operation(browser())
            if value is None:
                return jsonify(ok=False, error="Record not found"), 404
            return jsonify(ok=True, **value)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except (RuntimeError, sqlite3.Error):
            app.logger.exception("Trajectory inspection unavailable")
            return jsonify(ok=False, error="Trajectory memory is unavailable"), 503

    @app.get("/api/memory/trajectory-stats")
    def trajectory_stats():
        return respond(lambda b: {"stats": b.stats()})

    @app.get("/api/memory/episodes")
    def trajectory_episodes():
        return respond(lambda b: b.episodes(query=request.args.get("q", ""), **pagination()))

    @app.get("/api/memory/episodes/<episode_id>")
    def trajectory_episode(episode_id):
        return respond(lambda b: b.episode(episode_id))

    @app.get("/api/memory/transitions")
    def trajectory_transitions():
        return respond(lambda b: b.transitions(
            episode=request.args.get("episode_id"), agent=request.args.get("agent_id"),
            role=request.args.get("role"), query=request.args.get("q", ""),
            failed=request.args.get("failed") == "true", **pagination()))

    @app.get("/api/memory/transitions/<record_id>")
    def trajectory_transition(record_id):
        return respond(lambda b: ({"record": row} if (row := b.transition(record_id)) else None))
