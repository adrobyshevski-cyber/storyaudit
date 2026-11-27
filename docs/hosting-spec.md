# Спецификация хостинга: 1 статический сайт + 2 приложения уровня StoryAudit

Версия: 1.1 (2026-11-17). Основано на замерах этого репозитория (§0.12–0.13 HANDOFF.md),
а не на общих оценках.

---

## 0. Итог одной строкой

Один VPS: **2 vCPU / 8 GB RAM / 80 GB NVMe, Ubuntu 24.04 LTS, Docker Compose**.
Static-сайт — на Cloudflare Pages (бесплатно) либо тем же Caddy на этом же сервере.
Ориентир цены: **€6–9 или 700–1200 ₽/мес** за сервер + домен + LLM-расходы отдельно
(≈$0,05 за скан, 20–30 сканов/день → $30–45/мес).

Что **не** нужно: Kubernetes, отдельный сервер под БД, Redis, S3/объектное хранилище
(бэкапы — локально + снапшот провайдера, §10), балансировщик, GPU, 4+ vCPU.

Почему 8 ГБ, а не 4: RAM — единственный реально упирающийся ресурс (пик 3,9 ГБ при
двух одновременных PDF). CPU наоборот: одновременно рендерится максимум два PDF
(по одному на API), поэтому два ядра — это ровно по числу рендеров, а не «мало».

---

## 1. Профиль нагрузки (измерено)

| Параметр | Значение | Источник |
|---|---|---|
| Один скан (1 стр., `hybrid`) | 77–265 с, 2 вызова LLM, $0,0215–0,049 | `logs/capacity-10.json` |
| Полный `llm`-скан (12 стадий) | ~2350 с, $0,127 | §0.12 |
| 10 сканов подряд, 3 сайта | 0 × HTTP 429, среднее 271 с | §0.12 |
| API (uvicorn, 1 процесс) | RSS ≈ 32–76 МБ | `tasklist` |
| Worker (1 процесс) | RSS ≈ 75–150 МБ | `tasklist` |
| PostgreSQL, 1 БД с десятками сканов | 43 МБ данных / 278 МБ кластер | `pg_database_size` |
| PDF-снимок отчёта | 1–3 МБ, хранится в PostgreSQL | `app/retention.py` |
| Chromium (только API, для PDF) | ~0,5 ГБ на диске, 250–500 МБ RAM на рендер | `app/reports/render.py` |
| Рендер PDF | сериализован мьютексом, таймаут 60 с | `_render_lock` |
| Лимиты приложения | 100 сканов/сутки, 30 PDF/час, тело ≤ 2 МБ | `render.yaml` |

Вывод: нагрузка **I/O-bound** — процессы почти всё время ждут LLM и сеть.
CPU нужен короткими всплесками: один рендер PDF — это одно ядро на 10–20 с. Мьютекс
`_render_lock` допускает один рендер на процесс API, значит на двух приложениях —
максимум два одновременно: ровно 2 vCPU. Третье и четвёртое ядра простаивали бы 99 % времени.

---

## 2. Смета ресурсов на 2 приложения

Одно приложение = API + worker + своя БД. Оба приложения — один хост, один Postgres-кластер,
две базы.

| Компонент | CPU (покой / пик) | RAM steady | RAM peak | Диск |
|---|---|---|---|---|
| ОС + Docker + Caddy | 0,05 / 0,3 | 350 МБ | 600 МБ | 8 ГБ |
| PostgreSQL (кластер, 2 БД) | 0,1 / 0,5 | 700 МБ | 1,2 ГБ | 5–35 ГБ |
| API #1 (+Chromium) | 0,05 / 1,0 | 200 МБ | 900 МБ | 1,2 ГБ (образ+браузер) |
| API #2 (+Chromium) | 0,05 / 1,0 | 200 МБ | 900 МБ | 1,2 ГБ |
| Worker #1 | 0,1 / 0,5 | 150 МБ | 300 МБ | 0,6 ГБ |
| Worker #2 | 0,1 / 0,5 | 150 МБ | 300 МБ | 0,6 ГБ |
| **Итого** | **0,5 / 2,0** | **~1,75 ГБ** | **~3,9 ГБ** | **~13 ГБ + данные** |

Пик CPU 2,0 — это и есть два одновременных рендера PDF, по одному на приложение.
Пик RAM 3,9 ГБ — тот же сценарий: на 4 ГБ он впритык (OOM при любом всплеске),
на 8 ГБ остаётся запас под page cache PostgreSQL, который напрямую влияет на скорость
отчётов. Если PDF рендерит только одно приложение, 4 ГБ формально хватает —
но экономия €2–3 не окупает риск ночного OOM.

### Диск: почему 80 ГБ

- база: 8 ГБ ОС + 4 ГБ образы/venv/Chromium + 2 ГБ логи и запас;
- данные: PDF 3 шт./скан × 2 МБ × 30 сканов/день ≈ 180 МБ/день на приложение;
  retention 90 дней и «оставить 3 на скан» ограничивают это ≈ 16–20 ГБ на приложение;
- итого worst case ≈ 55 ГБ. 80 ГБ хватает, 100 ГБ — комфортно.
- Алерт при занятости > 70 %, автоочистка `RETENTION_ENABLED=true` обязательна.

---

## 3. Три варианта конфигурации

| | A. Минимум | **B. Рекомендуемый** | C. С запасом |
|---|---|---|---|
| vCPU / RAM | 2 / 4 ГБ | **2 / 8 ГБ** | 4 / 16 ГБ |
| Диск | 60 ГБ SSD | **80 ГБ NVMe** | 160 ГБ NVMe |
| Кого размещаем | 1 приложение + static | **1 static + 2 приложения** | 4 приложения, staging-копия |
| Что ломается | одновременный PDF в двух приложениях → OOM | ничего при 20–30 сканах/день | — |
| Цена, ориентир | €4–6 / 450–800 ₽ | **€6–9 / 700–1200 ₽** | €12–20 / 1500–2500 ₽ |

Вариант A допустим, только если PDF рендерит одно приложение или они не совпадают по времени.
Вариант B — целевой; дальше масштабируемся не сервером, а воркерами (§9).
Дороже 2/8 брать незачем: и CPU, и диск упираются в архитектуру (один рендер на процесс,
30 PDF/час), а не в объём железа.

---

## 4. Программный стек сервера

| Компонент | Версия | Зачем |
|---|---|---|
| Ubuntu 24.04 LTS (или Debian 12) | — | 5 лет обновлений, знакомый пакетный менеджер |
| Python | 3.12.x | как в проекте (`PYTHON_VERSION=3.12`) |
| PostgreSQL | 16.x | как в `compose.yaml`; в Docker — `postgres:16` |
| Caddy 2 | latest | TLS автоматически, reverse proxy, static |
| Docker Engine + Compose v2 | latest | изоляция двух приложений, лимиты памяти |
| Chromium (Playwright) | `playwright install chromium` | только в контейнере API |
| Chromium-зависимости | `playwright install-deps chromium` | libnss3, libatk, fonts и т.д. |

Альтернатива без Docker: venv + systemd-юниты (по одному на API и worker каждого
приложения). Docker предпочтительнее: два приложения с разными версиями зависимостей
не конфликтуют, а `mem_limit` защищает от OOM всего хоста.

---

## 5. Схема размещения

```
                      Cloudflare (DNS + TLS + WAF)
                                 │
                     ┌───────────┴────────────┐
                     │                        │
            static.example.com          app1.example.com
            (Pages, бесплатно)          app2.example.com
                                             │
                                    Caddy :443 (VPS)
                                    ├── app1-api  :8001 → 127.0.0.1
                                    └── app2-api  :8002 → 127.0.0.1
                                             │
                        ┌────────────────────┴───────────────┐
                        │ PostgreSQL 16 (только 127.0.0.1)   │
                        │   db storyaudit_app1               │
                        │   db storyaudit_app2               │
                        └────────────────────────────────────┘
                        worker1, worker2 (без портов, только исходящие)
```

Правила:

- наружу открыты только **22, 80, 443**; порты приложений и 5432 — только loopback/внутренняя сеть Docker;
- worker не публикует портов вообще;
- static-сайт не требует сервера: Cloudflare Pages + git-деплой. Если DNS не на Cloudflare —
  Caddy отдаёт `/srv/static` с `Cache-Control: public, max-age=3600` и gzip/brotli.

Домены:

| Хост | Что | TLS |
|---|---|---|
| `example.com`, `www` | статический сайт | Cloudflare / Let's Encrypt |
| `app1.example.com` | приложение 1 (API + UI + публичная форма) | Cloudflare proxy → Caddy |
| `app2.example.com` | приложение 2 | так же |

`PUBLIC_BASE_URL` в каждом `.env.production` — финальный HTTPS-origin своего поддомена
(влияет на canonical, `robots.txt`, `sitemap.xml`).

---

## 6. Раскладка каталогов и пользователи

```
/srv/storyaudit/
├── app1/                 # git clone, ветка main
│   ├── .env.production   # chmod 600, владелец deploy
│   ├── compose.yaml
│   └── backups/
├── app2/
└── static/               # только если static на этом же хосте

/var/lib/postgresql/      # volume контейнера postgres
/var/log/caddy/
```

- системный пользователь `deploy` (sudo, SSH-ключ), приложения — не от root;
- Postgres — отдельный кластер, две базы и две роли:
  `storyaudit_app1` / `storyaudit_app2`, владельцы своих баз, без `SUPERUSER` и без доступа к чужой базе;
- секреты: `.env.production` (600), копия в менеджере паролей; в git не попадает (`.gitignore`).

---

## 7. PostgreSQL

Один кластер на оба приложения: экономит ~700 МБ RAM на второй `shared_buffers`
и упрощает бэкап. Настройки для 8 ГБ хоста (файл `postgresql.conf` или `command:` в compose):

```conf
shared_buffers = 1GB
effective_cache_size = 3GB
work_mem = 16MB
maintenance_work_mem = 128MB
max_connections = 50
wal_compression = on
log_min_duration_statement = 1000   # медленные запросы в лог
```

- `max_connections=50` достаточно: API — пул на 5–10 соединений, worker — 1–2;
- бэкап `pg_dump -Fc` работает при настройках по умолчанию, WAL-архив не нужен;
- очереди jobs используют `FOR UPDATE SKIP LOCKED` — второй worker безопасен (§9);
- миграции: `alembic upgrade head` одноразовым контейнером перед стартом API (pre-deploy шаг).

---

## 8. PDF и Chromium — отдельные требования

PDF — единственная ресурсоёмкая операция в API.

- Chromium нужен **только** образу API (worker PDF не рендерит) — так и в `render.yaml`;
- в контейнере: `shm_size: 512mb` (иначе Chromium падает), флаг `--no-sandbox` уже передаётся кодом;
- `mem_limit: 1g` на API: рендер укладывается в 900 МБ, при выходе за лимит контейнер
  перезапускается, а не убивает хост;
- `REPORT_PDF_TIMEOUT_SECONDS=60` оставить; рендер сериализован мьютексом, поэтому
  два одновременных PDF на одном API не удваивают память, а ждут в очереди;
- если PDF-нагрузка станет частой (десятки в час) — вынести рендер в отдельный сервис,
  но для 30 PDF/час это не требуется.

---

## 9. Масштабирование и триггеры

| Триггер | Действие |
|---|---|
| > 50 сканов/сутки на приложение, очередь растёт | +1 процесс worker (тот же образ, `SKIP LOCKED` делает это безопасным) |
| PDF-рендеры мешают API (латентность > 2 с) | вынести рендер в отдельный контейнер/очередь |
| `pg_database_size` > 10 ГБ | проверить retention; при необходимости 40 ГБ disk и `RETENTION_REPORT_ARTIFACTS_DAYS=60` |
| RAM > 80 % постоянно | вариант C (16 ГБ) или разнести приложения по хостам |
| Появился третий/четвёртый проект | тот же VPS при 16 ГБ; дальше — второй сервер + общий Caddy |
| Нужен staging | отдельная база + отдельный поддомен `stage.*`, тот же хост |

Вертикального масштабирования достаточно до ~4–6 приложений: узкое место — LLM-латентность,
а не CPU.

---

## 10. Бэкапы: S3 не нужен

Внешняя копия нужна ровно от одного сценария: «сервер и бэкап умерли вместе»
(отказ диска, потеря VPS, блокировка аккаунта провайдера). Объектное хранилище —
лишь один из способов это закрыть, и для двух приложений он избыточен: дамп сжатой
базы — **30–50 МБ**, его некуда «складировать», его достаточно просто унести с машины.

Что действительно незаменимо: `llm_usage` (учёт расходов), история аннотаций и уже
выданные клиентские отчёты. Сам корпус пересобирается повторным сканом за $0,05,
поэтому потеря базы — это потеря истории, а не продукта. Отсюда и скромные требования.

Базовый вариант, без S3 и без второго аккаунта:

| Что | Как | RPO / RTO |
|---|---|---|
| Базы | `pg_dump -Fc` ежедневно 03:30 → `/srv/backups`, 7 daily + 4 weekly, `chmod 600` | 24 ч / 1–2 ч |
| Снапшот провайдера | еженедельно, если входит в тариф (обычно да) — это и есть копия на другом железе | 7 дней / 15 мин |
| Ручная копия | раз в месяц `scp` последнего дампа на домашний ПК (он уже есть, §0.11) | 30 дней |
| Секреты | `.env.production` в менеджере паролей | — |
| Код, Caddyfile, compose | git | — |

Команда дампа (в cron/systemd-timer, обе базы одним файлом):

```bash
pg_dump -Fc -d storyaudit_app1 -d storyaudit_app2 -f /srv/backups/sa-$(date +%F).dump
# отправка на домашний ПК, если он доступен по SSH через туннель
scp /srv/backups/sa-$(date +%F).dump home:/backups/
```

Проверка restore — раз в месяц: поднять пустую базу, `pg_restore`, прогнать
`alembic current` и `GET /health/ready`. Бэкап без проверки восстановления не считается.

Когда добавлять offsite (B2/Yandex Object Storage, 0–1 $/мес, либо второй VPS за €3–4):
когда появились платящие клиенты, счета или usage-записи, которые нельзя восстановить
пересчётом. Тогда тот же дамп уезжает через `restic`/`rclone` с шифрованием `age` —
это 5 строк конфига, а не смена архитектуры.

---

## 11. Безопасность (чек-лист)

- [ ] SSH: только ключи, `PermitRootLogin no`, `PasswordAuthentication no`, смена порта по желанию;
- [ ] `ufw`: `allow 22,80,443`, остальное `deny`; 5432 и 8001/8002 наружу не публикуются;
- [ ] `fail2ban` для SSH; `unattended-upgrades` для security-патчей;
- [ ] `APP_ACCESS_TOKEN` — 48+ случайных байт (`secrets.token_urlsafe(48)`), не `local-test-token`;
- [ ] `PUBLIC_HASH_SALT` — случайный, отдельный на каждое приложение;
- [ ] `WEBSITE_ALLOW_PRIVATE_HOSTS=false` (защита от SSRF в краулере);
- [ ] `LLM_API_KEY` есть только у worker, не у API;
- [ ] Cloudflare: proxy on, WAF + rate limit на публичную форму (`/public/*`);
- [ ] `MAX_REQUEST_BODY_BYTES=2097152` и `RATE_LIMIT_ENABLED=true` — как в `render.yaml`;
- [ ] логи не содержат токенов и тел запросов (проверить `LOG_LEVEL=INFO`).

---

## 12. Мониторинг и алерты

Внешний uptime-чек (healthchecks.io, UptimeRobot) — обязательно вне самого сервера:

| Проверка | Порог |
|---|---|
| `GET https://app1…/health/live` | 200, каждые 2 мин |
| `GET https://app1…/health/ready` | 200 (БД доступна), каждые 5 мин |
| Диск `/` | > 70 % → предупреждение, > 85 % → алерт |
| `pg_database_size` | рост > 1 ГБ/неделю → проверить retention |
| Очередь jobs | > 20 минут в статусе `pending` → алерт |
| Расход LLM | сумма `llm_usage.cost_usd` за сутки > $5 → предупреждение |

SQL для очереди и расхода:

```sql
SELECT status, count(*) FROM jobs
WHERE created_at > now() - interval '1 day' GROUP BY status;

SELECT sum(cost_usd), count(*) FROM llm_usage
WHERE created_at > now() - interval '1 day';
```

Логи: stdout контейнеров → journald, ротация `max-size=50m, max-file=5`.
Агрегатор (Loki/Grafana) для двух приложений избыточен; достаточно `docker logs` + journald.

---

## 13. Деплой: порядок операций

1. `git pull` в `/srv/storyaudit/appN`;
2. `docker compose build`;
3. `docker compose run --rm api alembic upgrade head`;
4. `docker compose up -d`;
5. `curl -fsS https://appN…/health/ready` — 200;
6. smoke: `python scripts/smoke_test.py` против домена (не тратит LLM, если `LLM_PROVIDER=mock`
   в тестовой базе) и один реальный скан раз в неделю.

Откат: `git checkout <prev-tag>` + `compose build && up -d`; миграции — только вперёд,
поэтому перед деплоем со схемой — бэкап `pg_dump`.

---

## 14. Стоимость (ориентир, ноябрь 2026)

| Статья | В месяц |
|---|---|
| VPS 2/8/80 NVMe | €6–9 / 700–1200 ₽ |
| Домен | €1 / 100–300 ₽ |
| Cloudflare (DNS, TLS, WAF, Pages) | 0 |
| Бэкапы (локально + снапшот провайдера) | 0 |
| LLM на 20–30 сканов/день × 2 приложения | $60–90 (см. ниже) |
| **Итого без LLM** | **≈ €7–11 / 800–1500 ₽** |

LLM — главная переменная, а не железо: $0,045–0,05 за `hybrid`-скан и $0,127 за полный `llm`.
При 40 сканах/сутки суммарно это $55–150/мес. Рычаги: `ANALYSIS_MODE=hybrid`,
`ANALYSIS_LLM_AGENTS=NARRATIVE,FINDINGS`, кэш ответов (`LLM_CACHE_ENABLED=true`),
лимиты `RATE_LIMIT_SCANS_PER_DAY`.

Нужная конфигурация — именно **2 vCPU / 8 ГБ / 80 ГБ**: Hetzner CX31, Timeweb Cloud,
Selectel, Aeza, Beget. Для RU/BY-оплаты выбирать из последних; Hetzner — карта/PayPal.
Если у провайдера нет 2/8, брать 2/4 только под один проект или 4/8 как ближайший
эквивалент — переплата небольшая, но 4 ядра не дадут ничего, кроме запаса.

Managed PaaS (Render: 2 web + 2 worker starter + 2 БД) обойдётся в $45–70/мес без
преимуществ для этой нагрузки, поэтому VPS предпочтительнее.

---

## 15. Приёмка

```bash
# снаружи
curl -fsS -o /dev/null -w '%{http_code}\n' https://app1.example.com/health/live   # 200
curl -fsS -o /dev/null -w '%{http_code}\n' https://app1.example.com/ui/scans       # 401
curl -fsS -o /dev/null -w '%{http_code}\n' https://app2.example.com/health/ready   # 200
curl -fsS https://static.example.com/ | head -5

# на сервере
docker compose ps                       # все up, healthcheck ok
free -m                                 # available > 2 ГБ
df -h /                                 # use < 70 %
docker stats --no-stream                # ни один контейнер не у лимита
ss -lntp                                # наружу только 22/80/443

# данные
psql -c "select datname, pg_size_pretty(pg_database_size(datname)) from pg_database;"
psql -c "select count(*) from jobs where status='pending' and created_at < now()-interval '20 minutes';"  # 0

# бэкап
ls -la backups/ | tail -3
```

Готово к клиентам, когда: оба health-чека зелёные снаружи, PDF скачивается по токену,
бэкап за последние 24 ч существует и однажды восстановлен, диск < 70 %.

---

## 16. Приложение: скелет `compose.yaml` хоста

```yaml
name: storyaudit-host

x-app: &app
  build: .
  restart: unless-stopped
  env_file: .env.production
  depends_on:
    postgres:
      condition: service_healthy
  logging:
    driver: json-file
    options: { max-size: "50m", max-file: "5" }

services:
  postgres:
    image: postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_USER: storyaudit
      POSTGRES_PASSWORD_FILE: /run/secrets/pg_password
      POSTGRES_DB: storyaudit_app1
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./postgresql.conf:/etc/postgresql/postgresql.conf:ro
    command: postgres -c config_file=/etc/postgresql/postgresql.conf
    secrets: [pg_password]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U storyaudit"]
      interval: 5s
      retries: 20
    # порт наружу не публикуем

  app1-api:
    <<: *app
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=*
    environment:
      ENV_FILE: /app/.env.production
    mem_limit: 1g
    shm_size: 512mb
    expose: ["8000"]

  app1-worker:
    <<: *app
    command: python -m app.worker
    mem_limit: 512m

  app2-api:
    <<: *app
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=*
    mem_limit: 1g
    shm_size: 512mb
    expose: ["8000"]

  app2-worker:
    <<: *app
    command: python -m app.worker
    mem_limit: 512m

  caddy:
    image: caddy:2
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - ./static:/srv/static:ro

volumes: { pgdata: {}, caddy_data: {} }
secrets:
  pg_password:
    file: ./secrets/pg_password
```

Caddyfile:

```
app1.example.com {
    encode zstd gzip
    reverse_proxy app1-api:8000
}

app2.example.com {
    encode zstd gzip
    reverse_proxy app2-api:8000
}

example.com, www.example.com {
    root * /srv/static
    file_server
    header Cache-Control "public, max-age=3600"
}
```

Второе приложение получает свой `DATABASE_URL` (`/storyaudit_app2`) и свой
`.env.production`; проще держать два каталога (`app1/`, `app2/`) с независимыми
compose-файлами и общим внешним volume для Postgres, чем один файл с восемью сервисами.

---

## 17. Что остаётся за рамками

- мультитенантность/биллинг — сейчас одно приложение = один тенант с bearer-токеном;
- объектное хранилище под PDF — пока PDF лежит в PostgreSQL, при росте > 20 ГБ
  переносить в S3-совместимое хранилище или отдельный диск (это про ёмкость, не про бэкапы);
- CDN для API-домена — Cloudflare proxy уже покрывает статику и TLS;
- HA/репликация БД — избыточно до первых платящих клиентов; бэкап + 1–2 ч RTO достаточно.
