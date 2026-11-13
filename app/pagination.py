"""Limit/offset pagination for list endpoints.

Lists keep returning a bare JSON array (clients and the UI depend on that), and the
paging metadata travels in headers:

* ``X-Total-Count`` — total rows that match, ignoring limit/offset
* ``X-Limit`` / ``X-Offset`` — what was applied
* ``Link`` — ``rel="next"`` when there is another page

``limit`` is capped rather than rejected: an over-eager client asking for 10 000
rows gets ``MAX_LIMIT`` instead of a 422.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True)
class Page:
    limit: int
    offset: int

    def headers(self, request: Request, total: int) -> dict[str, str]:
        headers = {
            "X-Total-Count": str(total),
            "X-Limit": str(self.limit),
            "X-Offset": str(self.offset),
        }
        if self.offset + self.limit < total:
            headers["Link"] = f'<{self._url(request, self.offset + self.limit)}>; rel="next"'
        if self.offset > 0:
            previous = max(0, self.offset - self.limit)
            parts = [headers.get("Link"), f'<{self._url(request, previous)}>; rel="prev"']
            headers["Link"] = ", ".join(part for part in parts if part)
        return headers

    def _url(self, request: Request, offset: int) -> str:
        query = dict(request.query_params)
        query.update({"limit": str(self.limit), "offset": str(offset)})
        return f"{urlsplit(str(request.url)).path}?{urlencode(query)}"


def page_params(
    limit: int = Query(DEFAULT_LIMIT, ge=1, description="Rows per page (capped at 200)"),
    offset: int = Query(0, ge=0, description="Rows to skip"),
) -> Page:
    return Page(limit=min(limit, MAX_LIMIT), offset=offset)


def paginate(session: Session, query: Any, page: Page) -> tuple[list[Any], int]:
    """Return ``(rows, total)``; the query must already be ordered."""
    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    rows = list(session.scalars(query.limit(page.limit).offset(page.offset)))
    return rows, total
