# Деплой на домашний ПК через Cloudflare Tunnel

Схема: PostgreSQL, API и worker работают на вашем ПК; наружу открыт только
исходящий туннель Cloudflare. Порт 8000 остаётся на `127.0.0.1` и в интернет не
публикуется.

```
Клиент → https://audit.example.com → Cloudflare → cloudflared → 127.0.0.1:8000
                                                              → worker → PostgreSQL
```

## 0. Что нужно

- Домен, добавленный в Cloudflare (бесплатный план).
- `cloudflared` для Windows.
- ПК, который не уходит в сон, с доступом в интернет.
- Заполненный `.env.production`.

## 1. Подготовить production-конфиг

```powershell
cd C:\work\narrative_audit
Copy-Item .env.production.example .env.production
```

Заполнить в `.env.production`:

| Переменная | Значение |
|---|---|
| `APP_ACCESS_TOKEN` | новый длинный токен, не `local-test-token` |
| `PUBLIC_HASH_SALT` | случайная строка |
| `PUBLIC_BASE_URL` | `https://audit.example.com` |
| `LLM_*` | скопировать из локального `.env` (включая `LLM_EXTRA_HEADERS`) |
| `WEBSITE_ALLOW_PRIVATE_HOSTS` | **`false`** |

Сгенерировать секреты:

```powershell
.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

`.env.production` уже в `.gitignore`. Права на файл оставьте только своему
пользователю Windows.

## 2. Проверить локально

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-public.ps1
```

Скрипт применит миграции, запустит API без `--reload` и worker с
`ENV_FILE=.env.production`, дождётся `/health/live`.

Проверки:

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8000/
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8000/ui/scans
```

Ожидается `200` и `401`. Если API не поднялся — `logs\api.err.log`, worker —
`logs\worker.err.log`.

Остановка: `powershell -ExecutionPolicy Bypass -File scripts\stop-dev.ps1`.

## 3. Быстрый тест туннеля

Вариант без установки и без администратора — портативный exe:

```powershell
New-Item -ItemType Directory -Force -Path C:\work\bin | Out-Null
Invoke-WebRequest `
  -Uri https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe `
  -OutFile C:\work\bin\cloudflared.exe
C:\work\bin\cloudflared.exe tunnel --url http://127.0.0.1:8000 --no-autoupdate
```

Либо `winget install --id Cloudflare.cloudflared` (может потребовать UAC).

Cloudflare выдаст `https://random-words.trycloudflare.com`. Это **только для
теста**: ссылку нельзя отправлять клиентам, она живёт до закрытия процесса и
меняется при перезапуске.

Если вместо URL появляется `failed to request quick Tunnel: context deadline
exceeded`, значит провайдер блокирует `api.trycloudflare.com`. Named tunnel
(раздел 4) использует другие endpoint'ы — `region1.v2.argotunnel.com:7844` — и
обычно работает. Проверка:

```powershell
Test-NetConnection region1.v2.argotunnel.com -Port 7844
```

Проверьте лендинг и отправьте одну тестовую заявку. Если не хотите тратить
реальный LLM-скан, остановитесь на проверке лендинга и `/ui/scans`.

## 4. Постоянный туннель и домен

```powershell
cloudflared tunnel login
cloudflared tunnel create storyaudit
cloudflared tunnel route dns storyaudit audit.example.com
```

`tunnel create` выведет UUID и создаст
`C:\Users\<you>\.cloudflared\<uuid>.json`. Скопируйте
`deploy\cloudflared-config.yml.example` в
`C:\Users\<you>\.cloudflared\config.yml` и подставьте UUID и домен.

Проверка:

```powershell
cloudflared tunnel run storyaudit
```

Затем откройте `https://audit.example.com`. После проверки установите туннель
как службу Windows (запуск от администратора):

```powershell
cloudflared service install
```

## 5. Защитить админку Cloudflare Access

Публичными должны остаться только:

- `/` — лендинг;
- `/audit/*` — статусы и отчёты по capability-токенам;
- `/static/*`, `/privacy`, `/robots.txt`, `/sitemap.xml`, `/sample-report`;
- `/health/live`.

Админку и API закройте Access:

1. Cloudflare Zero Trust → Access → Applications → Add self-hosted.
2. Домен `audit.example.com`, путь `ui` — policy allow только ваш email.
3. Второе приложение: путь `scans` — та же policy.
4. `APP_ACCESS_TOKEN` остаётся вторым фактором для `/ui` и API.

Бесплатный Access рассчитан до 50 пользователей.

## 6. Автозапуск и питание

Автозапуск через Планировщик заданий:

- «Выполнять независимо от входа пользователя»;
- триггер «При запуске»;
- действие:
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\work\narrative_audit\scripts\start-public.ps1`
- задержка 1–2 минуты, чтобы успел стартовать PostgreSQL.

Питание:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 15
```

Отключите «Быстрый запуск» Windows, если PostgreSQL не поднимается после
перезагрузки. Windows Update переводите в активные часы или ставьте обновления
вручную.

## 7. Бэкапы

```powershell
powershell -ExecutionPolicy Bypass -File scripts\backup-db.ps1
```

Дампы (`pg_dump -Fc`) складываются в `backups\`, хранятся 14 дней, каждый
проверяется через `pg_restore -l`. Поставьте задачу в Планировщике ежедневно.

Раз в месяц проверяйте восстановление в отдельную БД:

```powershell
& C:\work\pgsql\bin\createdb.exe -h 127.0.0.1 -U storyaudit storyaudit_restore_check
& C:\work\pgsql\bin\pg_restore.exe -h 127.0.0.1 -U storyaudit -d storyaudit_restore_check backups\storyaudit-YYYYMMDD-HHMMSS.dump
& C:\work\pgsql\bin\dropdb.exe -h 127.0.0.1 -U storyaudit storyaudit_restore_check
```

## 8. Production smoke

После смены `PUBLIC_BASE_URL` и перезапуска:

```powershell
$env:APP_ACCESS_TOKEN = "<токен из .env.production>"
python scripts\public_smoke.py `
  --base-url https://audit.example.com `
  --token $env:APP_ACCESS_TOKEN `
  --site-url https://www.issoft.by
```

Скрипт пройдёт лендинг → заявку → скан → Gate → HTML/PDF. Это один реальный
скан (~$0.12, до 15 минут).

## 9. Чек-лист перед первым клиентом

- [ ] `APP_ACCESS_TOKEN` заменён, в репозиторий не попал.
- [ ] `WEBSITE_ALLOW_PRIVATE_HOSTS=false`.
- [ ] `PUBLIC_HASH_SALT` случайный.
- [ ] `PUBLIC_BASE_URL` — финальный домен, не trycloudflare.
- [ ] Порт 8000 не проброшен в роутере и закрыт файрволом.
- [ ] Cloudflare Access стоит на `/ui` и `/scans`.
- [ ] Бэкап создан и восстановление проверено.
- [ ] Питание: сон и гибернация отключены.
- [ ] Туннель и стек стартуют после перезагрузки.
- [ ] `logs\api.err.log` и `logs\worker.err.log` не содержат ошибок.
- [ ] Одна тестовая заявка прошла до PDF, Gate утверждён в `/ui/scans`.

## 10. Переезд на сервер позже

Данные целиком лежат в PostgreSQL, PDF-снимки — в `report_artifacts`.
Переезд: `pg_dump` → `pg_restore` на новом сервере → тот же `.env.production`
с новым `DATABASE_URL` → переключить Cloudflare Tunnel на новый origin.
Домен и capability-ссылки клиентов при этом не меняются.
