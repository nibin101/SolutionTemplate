"""Row -> JSON helpers shared by the routers."""

from __future__ import annotations

import sqlite3
from typing import Any


def image_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "content_hash": row["content_hash"],
        "filename": row["filename"],
        "mime": row["mime"],
        "size_bytes": row["size_bytes"],
        "width": row["width"],
        "height": row["height"],
        "captured_at": row["captured_at"],
        "received_at": row["received_at"],
        "source": row["source"],
        "camera_make": row["camera_make"],
        "camera_model": row["camera_model"],
        "operatory": row["operatory"],
        "session_id": row["session_id"],
        "patient_id": row["patient_id"],
        "status": row["status"],
        "file_url": f"/api/images/{row['id']}/file",
        "thumb_url": f"/api/images/{row['id']}/thumb" if row["thumb_path"] else None,
    }


def patient_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "chart_number": row["chart_number"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "date_of_birth": row["date_of_birth"],
        "display_name": f"{row['first_name']} {row['last_name']}",
    }


def session_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = {
        "id": row["id"],
        "patient_id": row["patient_id"],
        "operatory": row["operatory"],
        "status": row["status"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "note": row["note"],
    }
    if "patient_name" in row.keys():
        data["patient_name"] = row["patient_name"]
    if "image_count" in row.keys():
        data["image_count"] = row["image_count"]
    return data
