"""Заполнить паспорт отчёта у уже собранных аудитов.

`run_scan` считает геометрию и категорию из корпуса, но сканы, созданные до
появления вывода, остались с пустым паспортом. Скрипт проходит по ним и
проставляет производные значения. Подтверждённые аналитиком поля не трогает.

    .venv\\Scripts\\python.exe scripts\\backfill_passport.py            # показать
    .venv\\Scripts\\python.exe scripts\\backfill_passport.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.analysis.passport import derive_passport, refresh_passport  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from app.models import Scan  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Заполнение паспорта отчёта из корпуса")
    parser.add_argument("--apply", action="store_true", help="записать изменения (по умолчанию dry-run)")
    parser.add_argument(
        "--all", action="store_true",
        help="все сканы (по умолчанию только сканы из публичных заявок)",
    )
    args = parser.parse_args()

    session = get_session_factory()()
    try:
        query = select(Scan).order_by(Scan.created_at)
        if not args.all:
            # в локальной базе живут и тестовые сканы: по умолчанию трогаем только
            # те, что пришли из публичных заявок (их отчёты видят клиенты)
            from app.models import AuditRequest

            query = select(Scan).where(
                Scan.id.in_(select(AuditRequest.scan_id).where(AuditRequest.scan_id.is_not(None)))
            ).order_by(Scan.created_at)
        scans = list(session.scalars(query))
        touched = 0
        for scan in scans:
            if scan.category_status == "confirmed":
                continue

            derived = derive_passport(session, scan)
            geography = derived["geography"]["value"]
            category = derived["category"]["value"]
            if not geography and not category:
                continue
            touched += 1
            print(
                f"{scan.id} {scan.title[:40]:<40} "
                f"география: {scan.geography or '-'} -> {geography or '-'}; "
                f"категория: {scan.category or '-'} -> {category or '-'}"
            )
            if args.apply:
                refresh_passport(session, scan)
        if args.apply:
            session.commit()
            print(f"\nобновлено сканов: {touched}")
        else:
            session.rollback()
            print(f"\nкандидатов: {touched}. Повторите с --apply, чтобы записать.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
