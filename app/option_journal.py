"""Options trading journal: every leg a person puts on in the Options Simulator is recorded here and kept for good.

The simulator keeps only the positions currently on the screen (user_sim_positions). Each time those are saved, this module compares them
with the journal: a new leg becomes an OPEN row, a leg that was exited becomes CLOSED with its realised P&L, and a leg that disappears
without an exit (the person cleared the screen) becomes REMOVED. Clearing the simulator therefore never loses history.

The person can add a note, tags and a 1-5 rating to any trade. Admins see the same journal read-only through "view as".
Paper trading only; nothing here reaches a broker.
"""
from __future__ import annotations

import csv
import io
import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.auth_api import db, require
from app.security import authz
from app.security.users import AuthError

router = APIRouter()
LOCAL_USER = "local"


def _uid(request: Request) -> str:
    u = getattr(request.state, "user", None)
    return u["id"] if u else LOCAL_USER


def _num(v, default=None):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def sync(c, user_id: str, symbol: str, positions: list[dict], now: float | None = None) -> None:
    """Bring the journal in line with the positions just saved for (user, symbol). Never raises on odd input: a journal problem must not stop a save."""
    now = time.time() if now is None else now
    seen: set[int] = set()
    for p in positions:
        try:
            side, opt, strike = str(p["side"]), str(p["opt"]), float(p["strike"])
            entry = float(p["entry"])
            key = (user_id, symbol, str(p.get("id")), side, opt, strike, p.get("entry_date"), entry)
        except (KeyError, TypeError, ValueError):
            continue
        row = c.execute("SELECT id, status FROM option_trades WHERE user_id=? AND symbol=? AND pos_id=? AND side=? AND opt=? AND strike=? AND entry_date IS ? AND entry=?", key).fetchone()
        closed = bool(p.get("closed"))
        status = "CLOSED" if closed else "OPEN"
        vals = dict(lots=int(p.get("lots") or 0), lot_size=int(p.get("lot_size") or 0), expiry=p.get("expiry"), entry_time=p.get("entry_time"), sl=_num(p.get("sl")),
                    exit=_num(p.get("exit")), exit_date=p.get("exit_date"), exit_time=p.get("exit_time"), exit_reason=p.get("exit_reason"), realized=_num(p.get("realized")))
        if row is None:
            c.execute("INSERT INTO option_trades (user_id, symbol, pos_id, side, opt, strike, expiry, lots, lot_size, entry, entry_date, entry_time, exit, exit_date, exit_time, "
                      "exit_reason, sl, realized, status, opened_at, closed_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (user_id, symbol, str(p.get("id")), side, opt, strike, vals["expiry"], vals["lots"], vals["lot_size"], entry, p.get("entry_date"), vals["entry_time"],
                       vals["exit"], vals["exit_date"], vals["exit_time"], vals["exit_reason"], vals["sl"], vals["realized"], status, now, now if closed else None, now))
            seen.add(c.execute("SELECT last_insert_rowid()").fetchone()[0])
        else:
            seen.add(row["id"])
            c.execute("UPDATE option_trades SET lots=?, lot_size=?, expiry=?, entry_time=?, sl=?, exit=?, exit_date=?, exit_time=?, exit_reason=?, realized=?, status=?, "
                      "closed_at=CASE WHEN ?='CLOSED' THEN COALESCE(closed_at, ?) ELSE NULL END, updated_at=? WHERE id=?",
                      (vals["lots"], vals["lot_size"], vals["expiry"], vals["entry_time"], vals["sl"], vals["exit"], vals["exit_date"], vals["exit_time"], vals["exit_reason"],
                       vals["realized"], status, status, now, now, row["id"]))
    # legs that were on the screen before and are gone now, without having been exited
    for r in c.execute("SELECT id FROM option_trades WHERE user_id=? AND symbol=? AND status='OPEN'", (user_id, symbol)).fetchall():
        if r["id"] not in seen:
            c.execute("UPDATE option_trades SET status='REMOVED', closed_at=?, updated_at=? WHERE id=?", (now, now, r["id"]))
    c.commit()


def backfill(c) -> int:
    """Record legs that were saved before the journal existed (or while it was off). Safe to run on every start: rows already there are only updated."""
    n = 0
    for r in c.execute("SELECT user_id, symbol, data_json, updated_at FROM user_sim_positions").fetchall():
        try:
            positions = json.loads(r["data_json"])
        except ValueError:
            continue
        sync(c, r["user_id"], r["symbol"], positions, r["updated_at"])
        n += len(positions)
    return n


def _rows(c, user_id: str, symbol: str | None = None, status: str | None = None, limit: int = 2000) -> list[dict]:
    q, args = "SELECT * FROM option_trades WHERE user_id=?", [user_id]
    if symbol:
        q += " AND symbol=?"; args.append(symbol.upper())
    if status:
        q += " AND status=?"; args.append(status.upper())
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(limit, 5000)))
    out = []
    for r in c.execute(q, args):
        d = dict(r)
        d["tags"] = [t for t in (d["tags"] or "").split(",") if t]
        d["pnl"] = d["realized"] if d["status"] == "CLOSED" else None
        out.append(d)
    return out


def stats(rows: list[dict]) -> dict:
    closed = [r for r in rows if r["status"] == "CLOSED" and r["realized"] is not None]
    wins = [r["realized"] for r in closed if r["realized"] > 0]
    losses = [r["realized"] for r in closed if r["realized"] < 0]
    net = sum(r["realized"] for r in closed)
    gross_win, gross_loss = sum(wins), -sum(losses)
    by_day: dict[str, float] = {}
    for r in sorted(closed, key=lambda r: (r["exit_date"] or "", r["id"])):
        by_day[r["exit_date"] or "?"] = by_day.get(r["exit_date"] or "?", 0.0) + r["realized"]
    curve, run = [], 0.0
    for d, v in by_day.items():
        run += v
        curve.append({"date": d, "day": round(v, 2), "cum": round(run, 2)})
    return {"trades": len(rows), "open": sum(1 for r in rows if r["status"] == "OPEN"), "closed": len(closed), "removed": sum(1 for r in rows if r["status"] == "REMOVED"),
            "net": round(net, 2), "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "avg_win": round(gross_win / len(wins), 2) if wins else None, "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "best": max((r["realized"] for r in closed), default=None), "worst": min((r["realized"] for r in closed), default=None), "curve": curve}


@router.get("/api/me/option-journal")
def my_journal(request: Request, symbol: str | None = None, status: str | None = None, limit: int = 2000, c=Depends(db)):
    rows = _rows(c, _uid(request), symbol, status, limit)
    return {"trades": rows, "stats": stats(_rows(c, _uid(request), symbol, None, 5000))}


class NoteBody(BaseModel):
    note: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = Field(default=None, max_length=10)
    rating: int | None = Field(default=None, ge=1, le=5)


@router.put("/api/me/option-journal/{trade_id}")
def annotate(trade_id: int, body: NoteBody, request: Request, c=Depends(db)):
    uid = _uid(request)
    if c.execute("SELECT 1 FROM option_trades WHERE id=? AND user_id=?", (trade_id, uid)).fetchone() is None:
        raise AuthError(404, "NOT_FOUND", "No such trade in your journal.")
    tags = ",".join(sorted({t.strip().lower().replace(",", "")[:24] for t in (body.tags or []) if t.strip()})) if body.tags is not None else None
    c.execute("UPDATE option_trades SET note=?, tags=COALESCE(?, tags), rating=?, updated_at=? WHERE id=? AND user_id=?",
              ((body.note or "").strip() or None, tags, body.rating, time.time(), trade_id, uid))
    c.commit()
    return {"ok": True}


def _cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@router.get("/api/me/option-journal.csv")
def my_journal_csv(request: Request, c=Depends(db)):
    rows = _rows(c, _uid(request), None, None, 5000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "index", "side", "type", "strike", "expiry", "lots", "lot_size", "entry_date", "entry", "exit_date", "exit", "exit_reason", "status", "realized", "rating", "tags", "note"])
    for r in rows:
        w.writerow([_cell(x) for x in (r["id"], r["symbol"], r["side"], r["opt"], r["strike"], r["expiry"], r["lots"], r["lot_size"], r["entry_date"], r["entry"], r["exit_date"], r["exit"],
                                       r["exit_reason"], r["status"], r["realized"], r["rating"], ",".join(r["tags"]), r["note"])])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="options-journal.csv"'})


@router.get("/api/admin/users/{user_id}/option-journal")
def admin_journal(user_id: str, actor=Depends(require("users.view_as")), c=Depends(db)):
    from app.security import users
    t = users.get_user(c, user_id)
    if t is None or t["status"] == "DELETED":
        raise AuthError(404, "NOT_FOUND", "No such user.")
    if not authz.can_view(actor, t):
        raise AuthError(403, "FORBIDDEN", "You cannot view this account.")
    rows = _rows(c, user_id, None, None, 2000)
    return {"trades": rows, "stats": stats(rows)}
