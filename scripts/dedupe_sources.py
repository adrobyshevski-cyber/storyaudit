"""Схлопывание дублей S-реестра: один домен — один S-код.

Дубли появляются, когда один сайт попал в корпус дважды (`example.com` и
`https://www.example.com`, `http` и `https`, повторное добавление вручную).
Новые такие случаи блокирует `add_website_source`, а этот скрипт чинит уже
собранные аудиты: перенаправляет ссылки в регистрах на оставшийся код и
удаляет лишние строки `source_registry`.

По умолчанию — только показать, что будет сделано. Запись — с `--apply`.

    .venv\\Scripts\\python.exe scripts\\dedupe_sources.py --scan <uuid>
    .venv\\Scripts\\python.exe scripts\\dedupe_sources.py --scan <uuid> --apply
    .venv\\Scripts\\python.exe scripts\\dedupe_sources.py --all --apply
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import get_session_factory  # noqa: E402
from app.models import Scan  # noqa: E402
from app.registers.sources import merge_duplicate_sources, registry_duplicates  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Схлопывание дублей источников в S-реестре")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--scan", help="UUID скана")
    target.add_argument("--all", action="store_true", help="все сканы, где есть дубли")
    parser.add_argument("--apply", action="store_true", help="записать изменения (по умолчанию dry-run)")
    args = parser.parse_args()

    session = get_session_factory()()
    try:
        if args.scan:
            scan_ids = [uuid.UUID(args.scan)]
        else:
            scan_ids = list(session.scalars(select(Scan.id).order_by(Scan.created_at)))

        total = 0
        for scan_id in scan_ids:
            duplicates = registry_duplicates(session, scan_id)
            if not duplicates:
                continue
            report = merge_duplicate_sources(session, scan_id, dry_run=not args.apply)
            for item in report:
                total += 1
                print(
                    f"{scan_id} {item['host']}: оставляем {item['kept']}, "
                    f"схлопываем {', '.join(item['merged'])}, "
                    f"правки: {item['repointed'] or '—'}"
                )
            if not args.apply:
                session.rollback()

        if not total:
            print("дублей не найдено")
        elif not args.apply:
            print(f"\nнайдено групп: {total}. Повторите с --apply, чтобы применить.")
        else:
            print(f"\nсхлопнуто групп: {total}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
