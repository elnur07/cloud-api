# server.py
import os
from typing import List, Optional
from datetime import date, datetime

import psycopg2
import psycopg2.extras
from psycopg2.pool import SimpleConnectionPool

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# -------------------------
# Config / DB Connection
# -------------------------
DB_URL = os.getenv("DB_URL")  # e.g. Supabase "connection string"
if not DB_URL:
    raise RuntimeError("DB_URL env var missing")

POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))
POOL: SimpleConnectionPool = SimpleConnectionPool(
    POOL_MIN, POOL_MAX, DB_URL, sslmode="require"
)

def get_conn():
    try:
        return POOL.getconn()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB pool error: {e}")

def put_conn(conn):
    try:
        POOL.putconn(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass

# -------------------------
# FastAPI app
# -------------------------
app = FastAPI(title="Checklist API")

# Allow desktop apps anywhere (tighten later if you want)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["*"],
    allow_methods=["*"],
)

# -------------------------
# Schemas
# -------------------------
class OperatorUpdate(BaseModel):
    status: str
    feedback: str
    evidence_path: Optional[str] = None

class InspectorUpdate(BaseModel):
    acceptance: Optional[str] = None
    feedback: Optional[str] = None

class CapUpsert(BaseModel):
    description: str = ""
    # owner removed in your latest flow; keep for future compatibility if needed
    owner: Optional[str] = None
    target_date: Optional[str] = Field(
        default=None, description="DD.MM.YYYY or null"
    )

class CapStep(BaseModel):
    step_no: int
    step_text: str
    target_date: Optional[str] = Field(default=None, description="DD.MM.YYYY or null")

class CapStepsReplace(BaseModel):
    steps: List[CapStep]

# -------------------------
# Helpers
# -------------------------
def _parse_ddmmyyyy(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d.%m.%Y").date()
    except ValueError:
        return None

# -------------------------
# Health
# -------------------------
@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat() + "Z"}

# -------------------------
# Checklist Items
# -------------------------
@app.get("/checklists/{checklist_id}/items")
def get_checklist_items(checklist_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT number, reference, question, status, evidence,
                   operator_feedback, acceptance, inspector_feedback
            FROM checklist_items
            WHERE checklist_id = %s
            ORDER BY number ASC
            """,
            (checklist_id,),
        )
        rows = cur.fetchall()
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fetch items error: {e}")
    finally:
        try: cur.close()
        except Exception: pass
        put_conn(conn)

@app.post("/items/{checklist_id}/{number}/operator")
def update_operator(checklist_id: int, number: int, body: OperatorUpdate):
    conn = get_conn()
    try:
        cur = conn.cursor()
        if body.evidence_path is not None:
            cur.execute(
                """
                UPDATE checklist_items
                   SET status = %s,
                       evidence = %s,
                       operator_feedback = %s
                 WHERE checklist_id = %s AND number = %s
                """,
                (body.status, body.evidence_path, body.feedback, checklist_id, number),
            )
        else:
            cur.execute(
                """
                UPDATE checklist_items
                   SET status = %s,
                       operator_feedback = %s
                 WHERE checklist_id = %s AND number = %s
                """,
                (body.status, body.feedback, checklist_id, number),
            )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Item not found")
        conn.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Operator update error: {e}")
    finally:
        try: cur.close()
        except Exception: pass
        put_conn(conn)

@app.post("/items/{checklist_id}/{number}/inspector")
def update_inspector(checklist_id: int, number: int, body: InspectorUpdate):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE checklist_items
               SET acceptance = %s,
                   inspector_feedback = %s
             WHERE checklist_id = %s AND number = %s
            """,
            (body.acceptance, body.feedback, checklist_id, number),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Item not found")
        conn.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Inspector update error: {e}")
    finally:
        try: cur.close()
        except Exception: pass
        put_conn(conn)

# -------------------------
# CAP header
# -------------------------
@app.get("/caps/{checklist_id}/{item_number}")
def get_cap(checklist_id: int, item_number: int):
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT description, owner, target_date
            FROM caps
            WHERE checklist_id = %s AND item_number = %s
            """,
            (checklist_id, item_number),
        )
        row = cur.fetchone()
        return row or {}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Get CAP error: {e}")
    finally:
        try: cur.close()
        except Exception: pass
        put_conn(conn)

@app.post("/caps/{checklist_id}/{item_number}")
def upsert_cap(checklist_id: int, item_number: int, body: CapUpsert):
    conn = get_conn()
    try:
        cur = conn.cursor()
        td = _parse_ddmmyyyy(body.target_date)
        cur.execute(
            """
            UPDATE caps
               SET description = %s,
                   owner = %s,
                   target_date = %s,
                   updated_at = CURRENT_TIMESTAMP
             WHERE checklist_id = %s AND item_number = %s
            """,
            (body.description, body.owner, td, checklist_id, item_number),
        )
        if cur.rowcount == 0:
            cur.execute(
                """
                INSERT INTO caps (checklist_id, item_number, description, owner, target_date)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (checklist_id, item_number, body.description, body.owner, td),
            )
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Upsert CAP error: {e}")
    finally:
        try: cur.close()
        except Exception: pass
        put_conn(conn)

# -------------------------
# CAP steps
# -------------------------
@app.get("/caps/{checklist_id}/{item_number}/steps")
def get_cap_steps(checklist_id: int, item_number: int):
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT step_no, step_text, target_date
            FROM cap_steps
            WHERE checklist_id = %s AND item_number = %s
            ORDER BY step_no ASC
            """,
            (checklist_id, item_number),
        )
        return cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Get CAP steps error: {e}")
    finally:
        try: cur.close()
        except Exception: pass
        put_conn(conn)

@app.put("/caps/{checklist_id}/{item_number}/steps")
def replace_cap_steps(checklist_id: int, item_number: int, body: CapStepsReplace):
    """
    Replace all steps for the given item with the provided list (1..N).
    Matches desktop app behavior: we renumber by incoming order.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        # Clear existing
        cur.execute(
            "DELETE FROM cap_steps WHERE checklist_id = %s AND item_number = %s",
            (checklist_id, item_number),
        )
        # Insert new
        for idx, s in enumerate(body.steps, start=1):
            d = _parse_ddmmyyyy(s.target_date)
            cur.execute(
                """
                INSERT INTO cap_steps (checklist_id, item_number, step_no, step_text, target_date)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (checklist_id, item_number, idx, s.step_text, d),
            )
        conn.commit()
        return {"ok": True, "count": len(body.steps)}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Replace CAP steps error: {e}")
    finally:
        try: cur.close()
        except Exception: pass
        put_conn(conn)

@app.get("/caps/{checklist_id}/{item_number}/final-date")
def compute_cap_final_date(checklist_id: int, item_number: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT MAX(target_date)
            FROM cap_steps
            WHERE checklist_id = %s AND item_number = %s
            """,
            (checklist_id, item_number),
        )
        row = cur.fetchone()
        d = row[0] if row else None
        return {"final_date": d.isoformat() if d else None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Compute final date error: {e}")
    finally:
        try: cur.close()
        except Exception: pass
        put_conn(conn)
