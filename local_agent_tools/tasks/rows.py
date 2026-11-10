"""Shared Task row codecs using the caller's existing transaction."""

from copy import deepcopy
import json

from local_agent_tools.foundation.sanitize import Fault
from . import database as db, effects, records

HISTORICAL = {"operations", "history", "_facts", "_decisions"}


def operation_key(locator: dict) -> tuple:
    kind = locator["kind"]
    return kind, "local", locator["execution_id" if kind == "execution" else "change_id"]


def _key(locator: dict) -> str:
    return records.canonical(operation_key(locator)).decode()


def record_core(record: dict) -> dict:
    return {key: value for key, value in record.items() if key not in HISTORICAL}


def read_decision(connection, identity: str, stage_id: str, version: int) -> dict:
    row = connection.execute("SELECT payload,sha256 FROM stage_decisions WHERE task_id=? AND stage_id=? AND version=?",
                             (identity, stage_id, version)).fetchone()
    if row is None:
        raise Fault("corrupt_tasks", "A current stage lost its original decision version.")
    try:
        value = records.Decision.model_validate(db.decode(row["payload"], row["sha256"])).model_dump()
        if len(set(value["evidence"])) != len(value["evidence"]):
            raise ValueError("duplicate decision evidence")
        missing = connection.execute("""SELECT 1 FROM json_each(?) AS evidence LEFT JOIN operations AS op
            ON op.task_id=? AND op.id=evidence.value WHERE op.id IS NULL OR op.stage_id<>? LIMIT 1""",
            (json.dumps(value["evidence"]), identity, stage_id)).fetchone()
        if missing:
            raise ValueError("decision evidence lost its original operation")
        return value
    except ValueError as error:
        raise Fault("corrupt_tasks", "A stage decision has invalid original evidence.") from error


def read_current(connection, row) -> tuple[dict, dict]:
    value = db.decode(row["current"], row["sha256"])
    versions = {}
    try:
        if value["task_id"] != row["task_id"] or value["revision"] != row["revision"]:
            raise ValueError("current identity or revision differs")
        for stage in value["plan"]["stages"]:
            version = stage.pop("decision_version")
            if type(version) is not int or not 1 <= version <= value["revision"] or "evidence" in stage:
                raise ValueError("invalid decision version")
            decision = read_decision(connection, row["task_id"], stage["id"], version)
            if stage["state"] != decision["state"]:
                raise ValueError("decision state differs")
            stage["evidence"] = decision["evidence"]
            versions[stage["id"]] = version
        return records.Current.model_validate(value).model_dump(), versions
    except (ValueError, TypeError, KeyError) as error:
        raise Fault("corrupt_tasks", "Current Task authority or its decision references are invalid.") from error


def pack_current(connection, core: dict, prior: dict | None = None) -> dict:
    """Keep only immutable decision references in the bounded current-state row."""
    value = deepcopy(core)
    for stage in value["plan"]["stages"]:
        decision = {"state": stage["state"], "evidence": stage.pop("evidence")}
        version = (prior or {}).get(stage["id"])
        if version is None or read_decision(connection, core["task_id"], stage["id"], version) != decision:
            version = core["revision"]
            payload, sha = db.encoded(decision)
            connection.execute("INSERT INTO stage_decisions VALUES (?,?,?,?,?)",
                               (core["task_id"], stage["id"], version, payload, sha))
        stage["decision_version"] = version
    return value


def task_row(connection, identity: str):
    row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (identity,)).fetchone()
    if row is None:
        raise Fault("task_not_found", "No original Task exists for this ID.")
    return row


def decode_operation(row) -> dict:
    value = db.decode(row["payload"], row["sha256"])
    try:
        value = records.Operation.model_validate(value).model_dump()
        locator = value["locator"]
        indexes = {key: value[key] for key in ("id", "stage_id", "plan_revision", "epoch", "admitted_at_ms", "observed_at_ms")}
        indexes.update(kind=locator["kind"], machine=locator["machine"], effect_key=_key(locator),
                       pending=int(records.pending(value)), unresolved=int(effects.unresolved(value)),
                       outcome=(value["result"] or {}).get("status"),
                       locator_id=locator.get("execution_id", locator.get("change_id")))
        if any(row[key] != item for key, item in indexes.items()):
            raise ValueError("operation indexes differ")
        return value
    except (ValueError, TypeError, KeyError) as error:
        raise Fault("corrupt_tasks", "An original operation has invalid content or binding indexes.") from error


def validate_operation(operation: dict, current: dict) -> dict:
    value = records.Operation.model_validate(operation).model_dump()
    stages = {stage["id"]: stage for stage in current["plan"]["stages"]}
    if (value["stage_id"] not in stages or value["plan_revision"] > current["plan"]["revision"]
            or value["epoch"] > current["control"]["epoch"] or not current["created_at_ms"] <= value["admitted_at_ms"] <= current["updated_at_ms"]
            or value["observed_at_ms"] is not None and value["observed_at_ms"] > current["updated_at_ms"]):
        raise ValueError("operation authority binding differs")
    if current["format"] < 4 and any((value["result"] or {}).get(key) is not None for key in ("effect", "failure_code")):
        raise ValueError("format does not permit scoped results")
    if value["plan_revision"] == current["plan"]["revision"] and value["source_sha256"] != stages[value["stage_id"]]["source_sha256"]:
        raise ValueError("operation source differs")
    return value


def put_operation(connection, identity: str, operation: dict, ordinal: int) -> None:
    locator = operation["locator"]
    payload, sha = db.encoded(operation)
    connection.execute("""INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(task_id,id) DO UPDATE SET observed_at_ms=excluded.observed_at_ms,pending=excluded.pending,
      unresolved=excluded.unresolved,outcome=excluded.outcome,payload=excluded.payload,sha256=excluded.sha256,
      effect_key=excluded.effect_key""", (identity, operation["id"], ordinal, operation["stage_id"],
        operation["plan_revision"], operation["epoch"], locator["kind"], locator["machine"], _key(locator),
        operation["admitted_at_ms"], operation["observed_at_ms"], int(records.pending(operation)),
        int(effects.unresolved(operation)), (operation["result"] or {}).get("status"), payload, sha,
        locator.get("execution_id", locator.get("change_id"))))


def append_event(connection, identity: str, value: dict, *, update: dict | None = None,
                 snapshot_at: int | None = None) -> None:
    payload, sha = db.encoded(value)
    if len(payload.encode()) > records.MAX_EVENT_BYTES:
        raise ValueError("audit event exceeds its bound")
    row = connection.execute("UPDATE tasks SET history_count=history_count+1 WHERE task_id=? RETURNING history_count", (identity,)).fetchone()
    connection.execute("INSERT INTO task_events VALUES (?,?,?,?,?,?,?,?)", (identity, row[0],
        "snapshot" if snapshot_at is not None else value["kind"], snapshot_at if snapshot_at is not None else value["at_ms"],
        value.get("stage_id"), value.get("operation_id"), payload, sha))
    if update is not None:
        connection.execute("INSERT INTO task_updates VALUES (?,?,?)", (identity, update["id"], update["request_sha256"]))


def insert_record(connection, record: dict, *, sequence: int | None = None) -> None:
    identity = record["task_id"]
    core = records.Current.model_validate(record_core(record)).model_dump()
    payload, sha = db.encoded(pack_current(connection, core))
    stage_counts = {stage["id"]: 0 for stage in core["plan"]["stages"]}
    for operation in record["operations"]:
        stage_counts[operation["stage_id"]] += 1
    connection.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)", (identity, "structured", core["revision"],
        db.bump(connection) if sequence is None else sequence, payload, sha, (identity + " " + core["title"]).casefold(),
        json.dumps(core["anchors"]), len(record["operations"]), 0, json.dumps(stage_counts)))
    for ordinal, operation in enumerate(record["operations"], 1):
        put_operation(connection, identity, validate_operation(operation, core), ordinal)
    for event in record["history"]:
        append_event(connection, identity, event)
    connection.execute("INSERT INTO task_updates VALUES (?,?,?)", (identity, record["last_update"]["id"], record["last_update"]["request_sha256"]))


def put_legacy(connection, identity: str, value: dict, *, sequence: int | None = None) -> None:
    payload, sha = db.encoded(value)
    connection.execute("""INSERT INTO tasks(task_id,kind,revision,change_seq,current,sha256,title_key,anchors)
      VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET revision=excluded.revision,
      change_seq=excluded.change_seq,current=excluded.current,sha256=excluded.sha256,title_key=excluded.title_key,anchors=excluded.anchors""",
      (identity, "legacy", value["revision"], db.bump(connection) if sequence is None else sequence,
       payload, sha, (identity + " " + value["title"]).casefold(), json.dumps(value["anchors"])))
