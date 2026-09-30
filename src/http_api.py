from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__, "message": str(exc)})

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/items":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"items": service.list_items(role)})
                elif path == "/api/audit":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"events": service.audit(role)})
                elif path.startswith("/api/items/"):
                    parts = [p for p in path.split("/") if p != ""]
                    # parts: ['api','items',item_id,...]
                    if len(parts) < 3:
                        self._json(404, {"error": "not_found"})
                        return
                    item_id = int(parts[2])
                    actor, role = self._identity()
                    del actor
                    if len(parts) == 3:
                        self._json(200, service.get_item(item_id, role))
                    elif len(parts) == 4 and parts[3] == "records":
                        self._json(200, {"records": service.list_records(item_id, role)})
                    elif len(parts) == 4 and parts[3] == "renewals":
                        self._json(200, {"renewals": service.list_renewals(item_id, role)})
                    elif len(parts) == 4 and parts[3] == "equipment":
                        self._json(200, {"equipment": service.list_equipment(item_id, role)})
                    elif len(parts) == 4 and parts[3] == "snapshots":
                        self._json(200, {"snapshots": service.list_snapshots(item_id, role)})
                    else:
                        self._json(404, {"error": "not_found"})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                elif path.startswith("/api/items/"):
                    parts = [p for p in path.split("/") if p != ""]
                    if len(parts) < 3:
                        self._json(404, {"error": "not_found"})
                        return
                    item_id = int(parts[2])
                    # /api/items/{id}/records
                    if len(parts) == 4 and parts[3] == "records":
                        self._json(201, service.add_record(item_id, body, actor, role))
                    # /api/items/{id}/transition
                    elif len(parts) == 4 and parts[3] == "transition":
                        target = body.get("target")
                        expected = body.get("expected_version")
                        self._json(200, service.transition(
                            item_id, target, expected, actor, role))
                    # /api/items/{id}/renewals
                    elif len(parts) == 4 and parts[3] == "renewals":
                        self._json(201, service.submit_renewal(item_id, body, actor, role))
                    # /api/items/{id}/equipment
                    elif len(parts) == 4 and parts[3] == "equipment":
                        self._json(201, service.add_equipment(item_id, body, actor, role))
                    # /api/items/{id}/records/{record_id}/close
                    elif len(parts) == 6 and parts[3] == "records" and parts[5] == "close":
                        record_id = int(parts[4])
                        self._json(200, service.close_record(item_id, record_id, actor, role))
                    # /api/items/{id}/renewals/{renewal_id}/approve
                    elif len(parts) == 6 and parts[3] == "renewals" and parts[5] == "approve":
                        renewal_id = int(parts[4])
                        note = body.get("note")
                        self._json(200, service.approve_renewal(
                            item_id, renewal_id, actor, role, note))
                    # /api/items/{id}/renewals/{renewal_id}/resubmit
                    elif len(parts) == 6 and parts[3] == "renewals" and parts[5] == "resubmit":
                        renewal_id = int(parts[4])
                        note = body.get("note")
                        self._json(200, service.resubmit_renewal(
                            item_id, renewal_id, actor, role, note))
                    # /api/items/{id}/equipment/{equipment_id}  (POST 不支持，用 PUT)
                    else:
                        self._json(404, {"error": "not_found"})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_PUT(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                parts = [p for p in path.split("/") if p != ""]
                # /api/items/{id}/equipment/{equipment_id}
                if (len(parts) == 5 and parts[0] == "api" and parts[1] == "items"
                        and parts[3] == "equipment"):
                    item_id = int(parts[2])
                    equipment_id = int(parts[4])
                    self._json(200, service.update_equipment(
                        item_id, equipment_id, body, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
