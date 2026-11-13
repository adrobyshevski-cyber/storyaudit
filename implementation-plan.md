# StoryAudit: полный план реализации и запуска на Render

> **ВНИМАНИЕ: план v1.1 устарел в части клиентского отчёта.**
> Разделы про `A2/A3/A4/A6`, `metrics`/`capitals`/`indices`, `formulas_v2` (24 метрики),
> Contradiction Engine, Rewriting Engine и отчёты FREE/FULL заменены решением `report_v2`:
> `docs/architecture-report-v2.md`, `docs/report-v2-schema.md`, `HANDOFF.md` §0.1.
> Инфраструктурная часть (корпус, очередь, generations, leases, fencing, auth) остаётся в силе.

Версия плана: 1.1 (implementation-ready MVP)  
Исходная архитектура: `runtime-architecture.md`  
Цель: реализовать воспроизводимый backend, который после публикации репозитория можно развернуть на Render через Blueprint без ручной доработки инфраструктуры.

---

## 1. Целевой результат

После завершения работ система должна позволять:

1. Создать scan и загрузить подготовленные источники, документы и блоки.
2. Поставить анализ в очередь PostgreSQL.
3. Выполнить pipeline:

   ```text
   A0 → A1 → [A2, A3, A4] → A5
      → SourceProfileBuilder → A6 → A7 → A8a → A8b
   ```

4. Сохранить исходный корпус без возможности его последующего изменения.
5. Сохранить все версии LLM-аннотаций и отметить текущую версию.
6. Повторно анализировать одну proposition без повторного запуска всего scan.
7. Детерминированно строить source profile.
8. Пересчитывать metrics, capitals, indices, findings и recommendations без повторных LLM-вызовов, если изменились только формулы или веса.
9. Получить через API цепочку:

   ```text
   Metric → Finding → Quote → Proposition → Block → Document → Source
   ```

10. Переживать restart/redeploy API и worker без потери или дублирования задач.

---

## 2. Архитектура развёртывания на Render

Использовать три компонента:

| Компонент | Render type | Ответственность |
|---|---|---|
| `storyaudit-api` | Web Service | HTTP API, создание scans, чтение статусов и результатов |
| `storyaudit-worker` | Background Worker | Агенты, детерминированные вычисления, orchestration |
| `storyaudit-postgres` | Render PostgreSQL | Данные, аннотации, результаты, очередь и блокировки |

Принципы:

- API и worker используют один репозиторий и один commit.
- API не выполняет анализ в `BackgroundTasks`.
- Worker не открывает HTTP-порт.
- PostgreSQL является единственной очередью; Redis для MVP не нужен.
- Локальная файловая система не используется для постоянных данных.
- Логи выводятся в `stdout`/`stderr` в JSON-формате.
- Миграции выполняются один раз через `preDeployCommand` Web Service.
- Все ресурсы Render создаются в одном регионе.
- На первом запуске используется по одному экземпляру API и worker.

Для предсказуемого production-запуска выбрать платные instance types. Free-окружение не использовать как основной вариант из-за ограничений жизненного цикла сервисов, background workers и pre-deploy команд.

---

## 3. Границы MVP

### Входит

- PostgreSQL schema и Alembic migrations.
- Immutable corpus.
- Versioned annotations.
- PostgreSQL queue и один worker.
- Dependency orchestrator.
- Контракты и реализации `A0–A8b` по зафиксированной методологии MVP v1.
- Детерминированный Source Profile Builder.
- Targeted reanalysis и downstream invalidation.
- Metrics/Evidence API.
- Health checks, structured logs, retries и graceful shutdown.
- Простейший server-rendered UI в сдержанных нейтральных тонах.
- Автоматические тесты и Render Blueprint.

### Не входит в ядро

- Полноценная регистрация, пользователи, роли и организации.
- Multi-tenancy и RLS.
- Billing.
- Crawler.
- Object storage.
- Distributed event bus.
- Сложная SPA, React/Node build pipeline и отдельный frontend-сервис.
- Несколько provider-specific реализаций; MVP использует один OpenAI-compatible adapter и mock provider для тестов.

Эти функции добавляются вокруг готового runtime без изменения методологии.

---

## 4. Технологический стек

Рекомендуемый стек:

- Python 3.12, версия зафиксирована в `render.yaml`.
- FastAPI — HTTP API.
- Jinja2 — server-rendered HTML для простейшего UI.
- Vanilla CSS и небольшой vanilla JavaScript — стили и обновление статусов без frontend build.
- Uvicorn — ASGI server.
- PostgreSQL — хранение и очередь.
- SQLAlchemy 2.x — доступ к БД и транзакции.
- Alembic — миграции.
- Psycopg 3 — PostgreSQL driver.
- Pydantic v2 и pydantic-settings — схемы и конфигурация.
- Pytest — тесты.
- httpx — API integration tests.
- Ruff — lint и formatting checks.

Все зависимости зафиксировать конкретными версиями в `requirements.txt`. Для тестов отдельно использовать `requirements-dev.txt`.

Не добавлять Docker в обязательный путь запуска: нативный Python runtime Render проще для первого deploy. Docker понадобится только при появлении системных зависимостей, несовместимых с native runtime.

---

## 5. Структура репозитория

```text
narrative_audit/
├── app/
│   ├── __init__.py
│   ├── main.py                 # создание FastAPI app
│   ├── worker.py               # entry point: python -m app.worker
│   ├── config.py               # environment settings
│   ├── db.py                   # engine, session, transaction helpers
│   ├── logging_config.py
│   ├── errors.py
│   ├── models/                 # SQLAlchemy models
│   ├── schemas/                # Pydantic API/agent schemas
│   ├── repositories/           # запросы и persistence operations
│   ├── queue/
│   │   ├── service.py          # enqueue, complete, fail, retry
│   │   └── leases.py           # heartbeat и stale recovery
│   ├── pipeline/
│   │   ├── orchestrator.py
│   │   ├── dependencies.py
│   │   ├── invalidation.py
│   │   └── runner.py
│   ├── agents/
│   │   ├── base.py
│   │   ├── a0.py ... a8b.py
│   │   └── registry.py
│   ├── profiles/
│   │   └── source_profile.py
│   ├── calculators/
│   │   ├── metrics.py
│   │   ├── capitals.py
│   │   └── indices.py
│   ├── reanalysis/
│   │   └── service.py
│   ├── api/
│   │   ├── health.py
│   │   ├── ui.py               # HTML routes
│   │   ├── scans.py
│   │   ├── sources.py
│   │   ├── metrics.py
│   │   ├── annotations.py
│   │   └── reanalysis.py
│   ├── templates/              # Jinja2: layout, scans, scan detail, metric detail
│   └── static/
│       ├── app.css             # единая нейтральная тема
│       └── app.js              # только polling статуса и мелкие UI-действия
├── alembic/
│   ├── versions/
│   └── env.py
├── methodology/
│   ├── schemas/                # executable JSON Schema A0–A8b
│   ├── formulas_v1.json
│   └── fixtures/               # golden inputs/outputs
├── .github/
│   └── workflows/test.yml
├── compose.yaml                # только локальная PostgreSQL
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── concurrency/
│   └── e2e/
├── scripts/
│   └── smoke_test.py
├── alembic.ini
├── requirements.txt
├── requirements-dev.txt
├── pyproject.toml
├── render.yaml
├── runtime-architecture.md
└── implementation-plan.md
```

`app/main.py` не должен запускать worker, scheduler или миграции при импорте. `app/worker.py` не должен импортировать и запускать Uvicorn.

---

## 6. Конфигурация

### Обязательные environment variables

| Переменная | API | Worker | Назначение |
|---|---:|---:|---|
| `DATABASE_URL` | Да | Да | Строка подключения Render PostgreSQL |
| `APP_ENV` | Да | Да | `production`, `development` или `test` |
| `PYTHON_VERSION` | Да | Да | Версия Python для Render build |
| `PORT` | Автоматически | Нет | Порт, выданный Render |
| `LOG_LEVEL` | Желательно | Желательно | По умолчанию `INFO` |
| `WORKER_ID` | Нет | Желательно | По умолчанию hostname + random suffix |
| `JOB_POLL_INTERVAL_SECONDS` | Нет | Желательно | По умолчанию `2` |
| `JOB_LEASE_SECONDS` | Нет | Желательно | Например, `300` |
| `JOB_HEARTBEAT_SECONDS` | Нет | Желательно | Например, `30` |
| `JOB_MAX_ATTEMPTS` | Нет | Желательно | По умолчанию `3` |

Дополнительные обязательные настройки:

| Переменная | API | Worker | Назначение |
|---|---:|---:|---|
| `APP_ACCESS_TOKEN` | Да | Нет | Единый секрет для Basic/Bearer access; задаётся через Render secret |
| `LLM_PROVIDER` | Нет | Да | `openai_compatible` для production, `mock` только для tests/smoke |
| `LLM_BASE_URL` | Нет | Да | Base URL OpenAI-compatible API, без ключа в логах |
| `LLM_MODEL` | Нет | Да | Конкретная версия модели |
| `LLM_API_KEY` | Нет | Да | Render secret, только worker |
| `MAX_REQUEST_BODY_BYTES` | Да | Нет | По умолчанию `2097152` (2 MiB) |
| `TEST_DATABASE_URL` | Только tests | Только tests | Отдельная тестовая PostgreSQL, production URL не используется |

Worker проверяет provider settings при старте. В production `LLM_PROVIDER=mock` запрещён; при отсутствии обязательных provider settings процесс завершается с понятной ошибкой до claim задач. Секреты не коммитить и не записывать в логи. Доступ к публичному API/UI защищается одним access token: API принимает `Authorization: Bearer ...`, browser UI использует HTTP Basic с username `audit` и token в качестве пароля. Это минимальная single-user защита, а не полноценная multi-user authentication.

### Подключение к БД

- Использовать internal connection string из Render database.
- Нормализовать URL под выбранный SQLAlchemy/Psycopg driver.
- Включить `pool_pre_ping=True`.
- Задать умеренный `pool_recycle`.
- Начать с небольшого pool: например, `pool_size=5`, `max_overflow=5` у API и меньший pool у worker.
- Не открывать постоянное соединение на этапе импорта модулей.
- Использовать короткие транзакции; не держать транзакцию БД открытой во время HTTP/LLM-запроса.

---

## 7. Зафиксированная методология MVP

Методология MVP v1, назначение агентов, payload, формулы, thresholds и confidence определены в **Приложении A**. Эти правила являются обязательной базовой реализацией, а не предметом самостоятельного выбора разработчика. Любое их изменение выполняется через новую methodology/schema/prompt/formula version.

До начала реализации агентов содержимое приложения переносится в исполняемые JSON Schema и versioned config без изменения смысла. Contract tests используют приведённые структуры и golden fixtures.

`A0`, `A5`, `A7`, `A8a`, `A8b` и Source Profile Builder детерминированы. LLM используется только в `A1`, `A2`, `A3`, `A4`, `A6`.

**Критерий завершения этапа:** схемы и config загружаются приложением, golden fixtures проходят validation, неизвестные поля отклоняются.

---

## 8. Модель данных PostgreSQL

Ниже зафиксирован physical contract MVP. Имена и обязательность полей являются частью реализации; изменения выполняются новой миграцией. UUID использует PostgreSQL `uuid`, timestamps — `timestamptz`, hashes — lowercase SHA-256 hex длиной 64.

### 8.1. Scans

`scans`:

- `id` — UUID, primary key;
- `title`;
- `status` — `draft/queued/running/succeeded/failed/cancelled`;
- `corpus_hash` — NULL в draft, immutable после run;
- `current_calculation_run_id` — NULL до первой публикации outputs;
- `methodology_version`;
- `schema_version`;
- `prompt_version`;
- `model_version`;
- `config_snapshot` — JSONB с формулами и весами;
- `created_at`, `updated_at`, `completed_at`;
- `error_summary` — без секретов и чувствительных payload.

Индексы: `status`, `created_at`.

### 8.2. Immutable corpus

`source`:

- `id` UUID;
- `scan_id` → `scans.id`;
- `name` TEXT NOT NULL;
- `type` TEXT NOT NULL; для MVP допустим `website`;
- `external_id` TEXT NULL;
- исходные metadata JSONB;
- `created_at`.

`document`:

- `id` UUID;
- `source_id` → `source.id`;
- `title` TEXT NOT NULL, `url` TEXT NULL, `content_hash` TEXT NOT NULL;
- `text` TEXT NOT NULL;
- metadata JSONB;
- `created_at`.

`block`:

- `id` UUID;
- `document_id` → `document.id`;
- `ordinal` — порядок внутри документа;
- текст;
- `start_offset` и `end_offset` INTEGER NOT NULL — offsets в document text, check `0 <= start_offset < end_offset`;
- `created_at`;
- unique `(document_id, ordinal)`.

`proposition`:

- `id` UUID;
- `block_id` → `block.id`;
- `ordinal`;
- текст proposition;
- `confidence` NUMERIC(5,4) NOT NULL, check `0 <= confidence <= 1`;
- `created_at`;
- unique `(block_id, ordinal)`.

Для всех четырёх таблиц создать PostgreSQL trigger function, которая вызывает исключение на `UPDATE` и `DELETE`. Триггеры проверяются integration tests под обычным application role.

Нельзя использовать каскадное удаление для очистки корпуса. Пересоздание источника означает добавление новой версии документа/scan, а не изменение старой записи.

### 8.3. Versioned annotations

`annotations`:

- `id` UUID;
- `scan_id`;
- `agent` — например, `A3`;
- `object_type` и `object_id`;
- `issue_id` — nullable, для targeted reanalysis;
- `analysis_generation` INTEGER NOT NULL;
- `scope_hash` TEXT NOT NULL;
- `payload` JSONB;
- `methodology_version`, `schema_version`, `prompt_version`, `model_version`;
- `supersedes_annotation_id` — self FK, nullable;
- `is_current` BOOLEAN;
- `created_at`.

Обеспечить:

- FK на анализируемый scan;
- проверку существования анализируемого объекта на уровне application service;
- частичный unique index `(scan_id, agent, object_type, object_id) WHERE is_current`;
- unique `supersedes_annotation_id` при non-NULL, чтобы у одной аннотации был максимум один replacement;
- запрет циклов и самоссылок в supersession chain;
- проверку совпадения scan/agent/object у predecessor и replacement через trigger;
- trigger `validate_annotation_object`, проверяющий существование polymorphic object и его принадлежность scan; application validation остаётся для понятных ошибок;
- индекс `(scan_id, agent, object_type, object_id, is_current)`.

Current-аннотация одна независимо от generation: generation определяет историю, но не входит в current unique key. Initial insertion и replacement используют один и тот же constraint.

Процедура replacement выполняется в одной короткой транзакции:

1. Взять transaction advisory lock по `(scan_id, agent, object_type, object_id)`, затем получить текущую аннотацию через `SELECT ... FOR UPDATE`. Lock сериализует concurrent replacements; после ожидания выполняется свежий SELECT current.
2. Проверить, что replacement ещё не существует.
3. Установить старой записи `is_current=false`.
4. Вставить новую запись с `is_current=true` и `supersedes_annotation_id` старой записи.
5. Зафиксировать транзакцию.

Если current-записи ещё нет, вставляется первая current-аннотация без `supersedes_annotation_id`. Порядок исключает конфликт partial unique index.

Старые аннотации не удаляются и не перезаписываются.

### 8.4. Jobs

`jobs`:

- `id` UUID;
- `scan_id`;
- `stage_run_id` → `stage_runs.id`;
- `analysis_generation` INTEGER NOT NULL;
- `agent`;
- `job_type` — `initial/reanalysis/recalculation`;
- `object_type`, `object_id`, `issue_id`;
- `reanalysis_request_id` UUID NULL;
- `scope_hash` TEXT NOT NULL;
- `idempotency_key` TEXT UNIQUE;
- `status` — `pending/running/succeeded/failed/cancelled/superseded`;
- `attempt_count`, `max_attempts`;
- `available_at`;
- `claimed_by`, `claimed_at`;
- `claim_token` UUID, новое значение при каждом claim;
- `lease_expires_at`, `heartbeat_at`;
- `started_at`, `finished_at`;
- `last_error_type`, `last_error_message`;
- `input_snapshot` JSONB NOT NULL — immutable snapshot/hashes входных объектов;
- `created_at`, `updated_at`.

Индексы:

- partial index для поиска `pending` по `available_at`;
- `(status, lease_expires_at)` для recovery;
- `(scan_id, status)`;
- `(agent, object_type, object_id)`;
- unique `idempotency_key`.

Idempotency keys формируются строго по архитектуре:

```text
initial:    {scan}:{agent}:{scopeHash}:{promptVersion}
reanalysis: {scan}:{agent}:{objectType}:{objectId}:{issueId}:{analysisGeneration}:{promptVersion}
retry:        {scan}:{agent}:{objectType}:{objectId}:{analysisGeneration}:{promptVersion}
recalculation: {scan}:{agent}:{calculationRunId}:{formulaVersion}
```

`scopeHash` вычисляется от канонической сериализации входных данных: стабильный порядок ключей, явные версии, исключение случайных значений и timestamps. HTTP idempotency обеспечивается `reanalysis_requests.client_request_id`; `analysis_generation` монотонно увеличивается для каждого нового подтверждённого request. Повтор того же client request возвращает исходный request/generation, новый client request создаёт новую generation даже для того же issue.

### 8.5. Recalculable outputs

Создать таблицы:

- `source_profiles`;
- `metrics`;
- `capitals`;
- `indices`;
- `findings`;
- `recommendations`.

Общие поля:

- `id`, `scan_id`;
- `methodology_version`;
- `formula_version` TEXT NOT NULL и `config_hash` TEXT NOT NULL;
- `calculation_run_id` → `calculation_runs.id`;
- `analysis_generation` INTEGER NOT NULL;
- `source_id` NULL для scan-wide outputs, NOT NULL для source-scoped;
- `is_current` BOOLEAN NOT NULL;
- `invalidated_at` TIMESTAMPTZ NULL;
- `payload` и численные значения;
- `created_at`.

Связующие таблицы:

- metric ↔ features/sources;
- finding ↔ metric;
- finding ↔ proposition;
- recommendation ↔ finding;
- output ↔ annotations, если необходимо воспроизвести вход.

Evidence links должны хранить конкретные proposition IDs. Quote и block/document/source provenance получаются через FK, а не копируются как непроверяемый текст.

При пересчёте новые outputs помечаются current, предыдущие — non-current с `invalidated_at`. История не удаляется. Current publication выполняется через единственный `scans.current_calculation_run_id`; все current outputs одного scan должны ссылаться на этот run. Partial unique indexes допускают один current output логического ключа и scan/source scope.

### 8.6. Stage, dependency, calculation и reanalysis tables

`stage_runs`:

- `id`, `scan_id`, `agent`, `analysis_generation`;
- `scope_type`, `scope_id`, `scope_hash`;
- `status` — `pending/running/succeeded/failed/cancelled/superseded`;
- `expected_job_count`, `completed_job_count`;
- `created_at`, `completed_at`;
- unique `(scan_id, agent, analysis_generation, scope_type, scope_id, scope_hash)`.

`job_dependencies`:

- `job_id`, `depends_on_job_id`;
- primary key `(job_id, depends_on_job_id)`;
- обе строки FK на jobs;
- запрет self-dependency и циклов при создании orchestration graph.

`calculation_runs`:

- `id`, `scan_id`, `analysis_generation`;
- `formula_version`, `config_hash`, `input_hash`;
- `status` — `running/succeeded/failed/superseded`;
- `created_at`, `completed_at`.

`reanalysis_requests`:

- `id`, `scan_id`, `client_request_id`, `issue_id`;
- `agent`, `object_type`, `object_id`;
- `analysis_generation`;
- `created_at`;
- unique `(scan_id, client_request_id)`.

`scans.analysis_generation` хранит последнюю выделенную generation. Выделение generation и создание reanalysis request выполняются в одной транзакции с блокировкой scan.

---

## 9. Миграции и целостность

Создать отдельные последовательные Alembic migrations:

1. Базовые extension/helper functions и scans.
2. Immutable corpus и triggers.
3. Annotations и индексы.
4. Jobs и `claim_next_job`.
5. Stage runs, dependencies, reanalysis requests и calculation runs.
6. Profiles, recalculable outputs и current publication.
7. Evidence links, generation constraints и publication triggers.
8. Дополнительные индексы после integration tests.

Требования:

- каждая миграция имеет рабочий `upgrade` и, где возможно, `downgrade`;
- `alembic upgrade head` успешно работает на пустой БД;
- повторный запуск на актуальной БД ничего не меняет;
- SQL functions и triggers устанавливаются миграциями, а не вручную;
- deploy не требует интерактивных команд.

---

## 10. Реализация очереди

### 10.1. Claim

SQL-функция `claim_next_job(worker_id)` должна:

1. Найти доступную задачу `pending`, у которой `available_at <= now()`.
2. Заблокировать строку через `FOR UPDATE SKIP LOCKED`.
3. Установить `running`, worker ID, timestamps и новый lease.
4. Увеличить `attempt_count`.
5. Вернуть задачу.
6. Выполнить всё в одной транзакции.

Порядок выбора: `available_at`, затем `created_at`, затем UUID для детерминированности.

### 10.2. Worker loop

```text
start
→ recover stale jobs
→ claim_next_job
→ после каждой обработанной задачи и периодически во время idle: recover stale jobs
→ если задачи нет: sleep poll interval
→ загрузить agent/handler
→ проверить актуальность задачи и scope
→ запустить heartbeat
→ выполнить handler без открытой DB transaction
→ валидировать результат
→ короткая транзакция: save result + complete job + enqueue downstream
→ повторить
```

Нельзя считать задачу успешной, если запись результата или постановка downstream задач не зафиксирована.

### 10.3. Retry и ошибки

Разделить ошибки:

- **retryable:** timeout, временная недоступность provider/DB, rate limit;
- **non-retryable:** невалидная схема, неизвестный agent, отсутствующий входной объект, методологическая ошибка;
- **cancelled:** задача относится к invalidated generation.

Для retryable ошибок:

- сохранить sanitized error;
- при наличии попыток вернуть `pending` с exponential backoff и небольшим jitter;
- после исчерпания попыток установить `failed`;
- не запускать downstream.

### 10.4. Lease и recovery

Пока выполняется длительная задача, background heartbeat обновляет `lease_expires_at`. Recovery считает зависшей только задачу с истёкшим lease.

При heartbeat, completion и failure UPDATE обязан содержать `job_id + claim_token + analysis_generation`. Нулевое число изменённых строк означает потерю ownership; результат старого worker не публикуется. Recovery выполняется периодически, а не только при старте. Если attempts исчерпаны, stale job становится failed; иначе — pending с новым `available_at`. При восстановлении lease создаётся новый `claim_token` при следующем claim.

### 10.5. Graceful shutdown

При `SIGTERM`:

- прекратить claim новых задач;
- остановить scheduling новых handlers;
- дать текущему handler ограниченное время на завершение;
- прекратить heartbeat при выходе;
- если завершить нельзя, оставить задачу для lease recovery;
- завершить процесс с корректным кодом.

---

## 11. Orchestrator

### 11.1. Initial scan и fan-out

1. API создаёт scan в `draft`; corpus можно добавлять только в этом статусе.
2. `POST /run` блокирует scan, запрещает дальнейшую загрузку, вычисляет `corpus_hash`, переводит scan в `queued` и атомарно создаёт `A0` stage/job.
3. `A0` создаёт по одному `A1` job на document.
4. После завершения всех `A1` orchestrator создаёт по одному job каждого `A2`, `A3`, `A4` на proposition. Все три группы могут выполняться параллельно.
5. `stage_runs.expected_job_count` фиксируется до публикации первого child job. Completion transaction увеличивает `completed_job_count` только при первом terminal success конкретной job.
6. Для каждого source `A5` ставится после success всех stage runs `A2/A3/A4` этого source. Join использует unique stage key и transaction lock; пустая группа считается completed сразу.
7. После всех `A5` создаются source-level Source Profile Builder jobs, затем по одному `A6` на source.
8. После всех `A6` создаются scan-level `A7 → A8a → A8b`.
9. После успешной публикации calculation run и всех outputs scan становится `succeeded`.

Точные scope rules приведены в Приложении A. `job_dependencies` хранит меж-этапные зависимости, `stage_runs` — fan-out barriers. Любой downstream job claimable только при успехе всех dependencies. Если scan не содержит документов, run отклоняется как validation error; если документ не содержит propositions, downstream profile строится как empty/unknown, а scan не зависает на пустом barrier.

Join `A2/A3/A4` должен быть защищён от гонки: завершение двух веток одновременно не должно создать два `A5`. Это обеспечивается idempotency key и транзакционной проверкой.

### 11.2. Agent handler

Единый интерфейс:

```text
load_context(job)
→ validate_input
→ execute
→ validate_output(JSON Schema)
→ persist_annotation
→ enqueue_downstream
```

В `AgentRegistry` допускаются только известные agent names. Версия prompt/model фиксируется в annotation. Детерминированные handlers используют тот же job lifecycle, но не создают видимость LLM-вызова.

### 11.3. Идемпотентность результата

Перед записи handler проверяет:

- job не cancelled;
- scope всё ещё актуален;
- результат с таким логическим ключом ещё не опубликован.

Повторная обработка уже succeeded job завершается без повторного анализа.

---

## 12. Source Profile Builder

Реализовать как чистую функцию без LLM и сетевых вызовов.

Вход:

- текущие аннотации указанного scan/source;
- версионированная конфигурация весов;
- `top_n`;
- tie-breaking policy.

Алгоритмы:

1. **Customer/problem/object/promise:** confidence-weighted mode.
2. **Hero и brand role:** confidence-weighted mode.
3. **Higher-order meanings:** сортировка по aggregated confidence/weight, затем top-N.
4. **Before/After:** взвешенные transformation labels.

Для weighted mode суммировать confidence по нормализованному label. При равенстве весов использовать стабильный tie-breaker, например нормализованную лексикографическую форму label, а не порядок ответа БД.

Фиксированные правила MVP:

- label нормализуется как Unicode NFC + trim + схлопывание внутреннего whitespace + lowercase; исходный display label сохраняется;
- confidence обязателен и должен находиться в `[0,1]`; NaN, Infinity, null и значения вне диапазона делают annotation invalid;
- annotations с `confidence < 0.20` исключаются из агрегации;
- при пустом входе scalar label = `unknown`, weight = `0`, списки пусты;
- `top_n=3`; сортировка по weight DESC, затем normalized label ASC;
- ties weighted mode разрешаются normalized label ASC;
- промежуточные суммы не округляются; публикуемые weights округляются Decimal `ROUND_HALF_UP` до 6 знаков.

Результат содержит sorted input annotation IDs и их `input_hash`. Одинаковые входные данные и конфигурация обязаны давать идентичный результат.

---

## 13. Targeted reanalysis и invalidation

### 13.1. Пример сценария

```text
A3 conflict(prop_17)
→ validate proposition и issue
→ enqueue A3 только для prop_17
→ insert replacement role annotation
→ rebuild affected source profile
→ invalidate/rerun A6
→ A7
→ A8a
→ A8b
```

### 13.2. Алгоритм API

Endpoint получает scan, agent, objectType/objectId, issueId и clientRequestId. Service:

1. Проверяет существование scan и объекта.
2. Разрешает targeted reanalysis только для `A2/A3/A4/A6/A7`; изменение corpus/A1 требует нового scan.
3. Под row lock scan ищет `(scan_id, client_request_id)`; повтор возвращает существующий request без новых jobs.
4. Для нового request увеличивает `analysis_generation`, сохраняет request и ставит scoped stage/jobs в той же транзакции.
5. Reanalysis key включает generation; поэтому новый request может повторно анализировать тот же issue, а retry HTTP не создаёт дубликат.
6. Не запускает полный initial pipeline.

### 13.3. Downstream graph

```text
A1       → A2,A3,A4,A5,A6,A7,A8
A2/A3/A4 → A5,A6,A7,A8
A5       → A6,A7,A8
A6       → A7,A8
A7       → A8
```

`A8` в operational graph разворачивается в `A8a → A8b`.

### 13.4. Защита от устаревших результатов

Каждому reanalysis присваивается новая монотонная `analysis_generation`. Issue ID остаётся стабильным идентификатором проблемы, request ID — идемпотентностью HTTP, generation — версией анализа. Задачи и outputs обязаны содержать generation. При старте downstream:

- старые ещё не запущенные jobs отменяются или помечаются superseded;
- running jobs завершаются только если их generation ещё актуальна;
- результат старой generation не может стать current;
- invalidated outputs остаются в истории;
- пересчёт ограничивается затронутым source/scope.

Не реализовывать invalidation как полный `DELETE` и полный replay scan.

---

## 14. Metrics, capitals, indices, findings, recommendations

Реализовать формулы из Приложения A как чистые функции.

Для каждого результата сохранять:

- входные значения и `input_hash` расчётного snapshot;
- formula version;
- weights/config version;
- precision и rounding policy;
- generation;
- source/feature breakdown;
- evidence links.

Порядок:

```text
Metric → Capital → Index → Finding → Recommendation
```

Правила:

- Изменение только формул/весов запускает deterministic graph `A7 → A8a → A8b` на зафиксированном A6 input snapshot без LLM. Source profiles и annotations не пересчитываются.
- Finding без обязательного evidence не публикуется как current.
- Recommendation связана с finding и не существует безосновательно.
- Новый calculation run сначала полностью материализуется как non-current. После успеха одна транзакция меняет `scans.current_calculation_run_id`, старый run получает `superseded`, его outputs — `invalidated_at`, новые outputs — `is_current=true`.
- Любая ошибка до publication оставляет предыдущий current run доступным.
- Не смешивать outputs разных generations в одном API response.

---

## 15. HTTP API

Минимальная версия:

| Метод | Endpoint | Назначение |
|---|---|---|
| `GET` | `/health/live` | Проверка процесса |
| `GET` | `/health/ready` | Проверка процесса и `SELECT 1` |
| `POST` | `/scans` | Создание scan |
| `GET` | `/scans/{scanId}` | Статус scan и сводка jobs |
| `GET` | `/scans/{scanId}/jobs` | Диагностика очереди |
| `POST` | `/scans/{scanId}/sources` | Добавление подготовленного input corpus |
| `POST` | `/scans/{scanId}/run` | Freeze corpus и постановка initial pipeline |
| `POST` | `/scans/{scanId}/retry` | Новая generation только для failed stage/downstream |
| `POST` | `/scans/{scanId}/reanalysis` | Targeted reanalysis |
| `GET` | `/scans/{scanId}/profile` | Current source profile |
| `GET` | `/scans/{scanId}/metrics` | Список current metrics |
| `GET` | `/scans/{scanId}/metrics/{metricId}` | Metric/Evidence response |
| `GET` | `/scans/{scanId}/annotations` | История/фильтры annotations |

Правила:

- UUID и payload валидируются Pydantic.
- Ошибки возвращаются в едином формате с `code`, `message`, `request_id`.
- Не возвращать stack traces, SQL, secrets и provider payloads клиенту.
- POST создания scan/run/reanalysis идемпотентны там, где это требуется.
- Запуск scan дважды не создаёт второй pipeline.
- Все list endpoints имеют limits/pagination там, где объём не ограничен.

### Metric/Evidence response

`GET /scans/{scanId}/metrics/{metricId}` возвращает:

1. Current metric result.
2. Feature/source breakdown.
3. Findings.
4. Exact evidence propositions.
5. Block/document/source provenance.

Пример структуры:

```json
{
  "metric": {"id": "...", "value": 0, "version": "..."},
  "breakdown": [],
  "findings": [
    {
      "id": "...",
      "evidence": [
        {
          "proposition": {"id": "...", "text": "..."},
          "block": {"id": "...", "ordinal": 1},
          "document": {"id": "...", "title": "..."},
          "source": {"id": "...", "name": "..."}
        }
      ]
    }
  ]
}
```

---

## 16. Простейший UI

### 16.1. Техническое решение

UI обслуживается тем же FastAPI Web Service, что и API:

- Jinja2 templates для HTML;
- один статический `app.css`;
- небольшой `app.js` только для polling активного scan и простых подтверждений;
- обычные HTML forms для создания scan, запуска pipeline и targeted reanalysis;
- никаких React, Node.js, сборщиков, отдельного Static Site или дополнительного Render service.

Корневой `/` перенаправляет на `/ui/scans`. JSON API остаётся основным программным интерфейсом, а HTML routes используют те же application services и не дублируют бизнес-логику.

### 16.2. Страницы

**`/ui/scans`**

- список scans со статусом, датой создания и версией методологии;
- кнопка «Создать scan»;
- короткая форма создания и добавления подготовленного источника;
- понятные empty/error states.

**`/ui/scans/{scanId}`**

- общий статус и кнопка «Запустить анализ»;
- компактный pipeline indicator;
- список jobs с agent, status, attempts и sanitized error;
- current source profile;
- карточки metrics с переходом к evidence;
- findings и recommendations;
- простая форма targeted reanalysis;
- polling каждые 3–5 секунд только пока scan или jobs активны.

**`/ui/scans/{scanId}/metrics/{metricId}`**

- текущее значение и версия метрики;
- feature/source breakdown;
- findings;
- evidence quotes в раскрывающихся блоках;
- provenance `Proposition → Block → Document → Source`;
- ссылка назад к scan.

Для MVP не добавлять сложные графики, редактор methodology, drag-and-drop, realtime WebSocket и обширную админ-панель.

### 16.3. Визуальный стиль

Стиль: сдержанный, нейтральный, рабочий, без ярких градиентов и декоративных иллюстраций.

| Назначение | Цвет |
|---|---|
| Фон страницы | `#F5F6F7` |
| Поверхность/card | `#FFFFFF` |
| Основной текст | `#20262E` |
| Вторичный текст | `#66707A` |
| Границы | `#DDE1E5` |
| Основной акцент/кнопки | `#4B6478` |
| Успех | приглушённый `#3F6B57` |
| Предупреждение | приглушённый `#80652F` |
| Ошибка | приглушённый `#854B4B` |

Правила оформления:

- системный font stack, базовый размер 14–16 px;
- контент ограничен шириной примерно 1200 px;
- белые cards с тонкой границей и минимальной или нулевой тенью;
- скругления 6–8 px;
- один основной accent color;
- статусы обозначаются не только цветом, но и текстом;
- таблицы и формы выравниваются по единой сетке;
- интерфейс остаётся читаемым на мобильной ширине;
- видимые keyboard focus states и контраст не ниже WCAG AA.

### 16.4. Поведение и безопасность

- Весь пользовательский и LLM-текст экранируется Jinja2 autoescape.
- Ошибки API отображаются коротким понятным сообщением без stack trace.
- Длительные операции не имитируют синхронный HTTP request: форма ставит job, после чего UI показывает статус.
- Form POST обрабатывается отдельными `/ui/.../actions/...` routes и завершается POST → Redirect → GET. JSON и HTML actions вызывают один application service.
- Browser получает stable `client_request_id` из hidden UUID; refresh после redirect безопасен.
- Form POST проверяет same-origin `Origin`/`Referer`; access token обязателен.
- Повторный submit блокируется после отправки, а серверная идемпотентность всё равно обязательна.
- Для running/failed jobs доступна диагностика без внутренних SQL и секретов.
- При отключённом JavaScript базовая навигация, формы и ручное обновление страницы продолжают работать.

## 17. Логирование и наблюдаемость

JSON log fields:

- timestamp;
- level;
- service (`api`/`worker`);
- environment;
- request ID;
- scan ID;
- job ID;
- agent;
- object type/id;
- duration;
- attempt;
- outcome.

Не логировать полные секреты, API keys, connection strings и необработанные чувствительные данные.

Минимальные operational metrics в логах или через последующий metrics adapter:

- jobs processed/failed/retried;
- queue depth;
- job duration;
- stale jobs recovered;
- LLM/provider failures;
- DB errors;
- active worker heartbeat.

Health live не зависит от БД. Ready делает короткий `SELECT 1` с timeout. Render health check направлен на `/health/live`.

---

## 18. Render configuration

В корень репозитория добавить `render.yaml`:

```yaml
services:
  - type: web
    name: storyaudit-api
    runtime: python
    region: oregon
    plan: starter
    buildCommand: pip install --upgrade pip && pip install -r requirements.txt
    preDeployCommand: alembic upgrade head
    startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT
    healthCheckPath: /health/live
    envVars:
      - key: PYTHON_VERSION
        value: 3.12.8
      - key: APP_ENV
        value: production
      - key: LOG_LEVEL
        value: INFO
      - key: DATABASE_URL
        fromDatabase:
          name: storyaudit-postgres
          property: connectionString
      - key: APP_ACCESS_TOKEN
        sync: false
      - key: MAX_REQUEST_BODY_BYTES
        value: "2097152"

  - type: worker
    name: storyaudit-worker
    runtime: python
    region: oregon
    plan: starter
    buildCommand: pip install --upgrade pip && pip install -r requirements.txt
    startCommand: python -m app.worker
    envVars:
      - key: PYTHON_VERSION
        value: 3.12.8
      - key: APP_ENV
        value: production
      - key: LOG_LEVEL
        value: INFO
      - key: DATABASE_URL
        fromDatabase:
          name: storyaudit-postgres
          property: connectionString
      - key: JOB_POLL_INTERVAL_SECONDS
        value: "2"
      - key: JOB_LEASE_SECONDS
        value: "300"
      - key: JOB_HEARTBEAT_SECONDS
        value: "30"
      - key: JOB_MAX_ATTEMPTS
        value: "3"
      - key: LLM_PROVIDER
        value: openai_compatible
      - key: LLM_BASE_URL
        sync: false
      - key: LLM_MODEL
        sync: false
      - key: LLM_API_KEY
        sync: false

databases:
  - name: storyaudit-postgres
    region: oregon
    plan: basic-256mb
```

Перед реальным deploy сверить названия тарифов и поддержку `preDeployCommand` с актуальными настройками Render. Если выбранный тариф не поддерживает pre-deploy command, миграция запускается как отдельная одноразовая операция `alembic upgrade head` до обновления API/worker. Не переносить миграцию в `startCommand`.

---

## 19. Пошаговый порядок разработки

### Шаг 1. Инициализация

- Перенести Приложение A в executable JSON Schema, versioned formulas config и golden fixtures.
- Создать структуру пакета, dependency files и линтер.
- Добавить settings и JSON logging.
- Создать пустые API/worker entry points.
- Добавить health endpoints.

**Готово:** API и worker локально стартуют независимо.

### Шаг 2. БД и миграции

- Реализовать models и migrations всех сущностей.
- Добавить triggers и indexes.
- Проверить чистый `upgrade head`.

**Готово:** пустая PostgreSQL полностью инициализируется одной командой.

### Шаг 3. Corpus и scans API

- Реализовать создание scan, загрузку source/document/block/proposition.
- Добавить content validation и transaction boundaries.

**Готово:** загруженный corpus доступен по provenance, но не может быть изменён SQL.

### Шаг 4. Queue

- Реализовать enqueue, claim, complete, fail, retry, heartbeat, recovery.
- Добавить worker loop и graceful shutdown.

**Готово:** concurrency tests подтверждают отсутствие double claim и потерянных задач.

### Шаг 5. Orchestrator на mock agents

- Реализовать graph и handlers с фиксированными mock results.
- Настроить параллельные `A2/A3/A4` и join перед `A5`.

**Готово:** полный scan проходит graph без LLM; `A5` создаётся один раз.

### Шаг 6. Versioned annotations

- Реализовать create/replacement/current semantics.
- Добавить schema validation и version fields.

**Готово:** повторный анализ сохраняет историю и единственную current-аннотацию.

### Шаг 7. Реальные агенты

- Реализовать контракты `A0–A8b` из Приложения A без изменения scope и формул.
- Подключить OpenAI-compatible provider через узкий adapter только там, где нужен LLM; mock provider реализует golden fixtures.
- `A8a` оставить детерминированным.

**Готово:** contract tests проходят без обращения к внешнему provider; provider integration tests отдельны.

### Шаг 8. Source Profile Builder

- Реализовать взвешенные алгоритмы, tie-breaking и конфигурацию.
- Подключить между `A5` и `A6`.

**Готово:** snapshot/property tests подтверждают воспроизводимость.

### Шаг 9. Targeted reanalysis

- Реализовать endpoint, graph invalidation, generation checks и ограничение scope.

**Готово:** conflict одной proposition не создаёт jobs для несвязанных объектов и не повторяет весь scan.

### Шаг 10. Calculators и evidence

- Реализовать формулы, версии конфигурации, findings/recommendations и evidence links.
- Добавить recalculation без LLM.

**Готово:** изменение весов создаёт новую calculation generation без новых annotations.

### Шаг 11. Read API

- Реализовать status, jobs, profiles, annotations и Metric/Evidence endpoints.
- Проверить согласованность current generation.

**Готово:** API возвращает полную цепочку до Source без ручных SQL-запросов.

### Шаг 12. Минимальный UI

- Добавить Jinja2 layout, три HTML-страницы, нейтральный CSS и ограниченный polling.
- Подключить HTML routes к существующим services без дублирования логики.
- Проверить формы, empty/error states и работу без JavaScript.

**Готово:** пользователь может создать scan, запустить анализ и пройти от metric до source через браузер.

### Шаг 13. Тестирование и hardening

- Выполнить тестовую матрицу из следующего раздела.
- Добавить timeouts, sanitized errors и operational logs.

### Шаг 14. Render smoke deployment

- Развернуть Blueprint на чистой БД.
- Выполнить smoke test и redeploy test.
- Только после этого подключать реальные объёмы данных.

---

## 20. Обязательная тестовая матрица

### Unit tests

- Canonical scope hash стабилен.
- Idempotency keys соответствуют формату.
- Weighted mode и top-N работают на граничных случаях.
- Tie-breaking детерминирован.
- Каждая формула metric/capital/index имеет примеры.
- Retry classifier правильно разделяет ошибки.
- Dependency graph возвращает правильный downstream set.

### Database integration tests

- UPDATE/DELETE corpus вызывают ошибку.
- Foreign keys запрещают разорванный provenance.
- Невозможно создать две current-аннотации одного логического объекта.
- Два concurrent replacements сериализуются без нарушения unique index и создают одну линейную history chain.
- Replacement сохраняет supersession chain.
- `alembic upgrade head` работает на пустой БД.
- Индексы и constraints присутствуют после миграций.

### Queue/concurrency tests

- Несколько workers не получают одну задачу.
- Повторный enqueue с тем же key возвращает существующую задачу.
- Повтор reanalysis request не меняет generation; новый request того же issue создаёт новую generation.
- Initial retry использует отдельный generation-scoped key и не конфликтует с failed job.
- `A5` ставится один раз при одновременном завершении веток.
- Fan-out на несколько documents/propositions завершает barrier только после всех jobs.
- Document/source без propositions не блокирует pipeline.
- Retry уважает `available_at` и max attempts.
- Истёкший lease восстанавливается.
- Неистёкший lease не перехватывается.
- Старый worker не может завершить задачу после потери lease.
- Crash до commit не публикует частичный результат.
- Crash после commit не создаёт дубликат downstream.

### Pipeline tests

- Полный initial graph выполняется в правильном порядке.
- Ошибка `A2` блокирует `A5`, scan становится failed, pending downstream отменяются.
- Retry failed scan не replay-ит succeeded stages.
- Targeted A3 затрагивает только нужный downstream path и использует snapshot остальных current annotations.
- Sources после run отклоняются с `409`, corpus hash остаётся неизменным.
- Invalidated generation не становится current.
- Recalculation, включая A8b recommendations, не вызывает LLM adapters.
- Missing production provider settings останавливают worker до claim; mock в production запрещён.

### API tests

- Health live работает без БД.
- Ready корректно сообщает о доступности БД.
- Повторный run не создаёт второй pipeline.
- Отсутствующий/неверный access token возвращает `401`; health endpoints остаются доступными.
- Body limits и same-origin form validation возвращают документированные ошибки.
- Невалидные UUID/payload возвращают единый error schema.
- Metric response содержит evidence и provenance.
- Старые outputs не смешиваются с current.

### UI tests

- Главная страница и scan detail возвращают HTML без обращения к внешнему frontend.
- Создание/run/retry/reanalysis через forms вызывают те же services, что и JSON API, и завершаются `303` PRG.
- Пользовательский текст и evidence корректно HTML-экранируются.
- Статусы, ошибки и пустые состояния отображаются понятным текстом.
- Polling прекращается после terminal status.
- Базовые страницы доступны с клавиатуры и не ломаются на узком экране.

### End-to-end smoke test

На чистой Render PostgreSQL:

1. Проверить `/health/live` и `/health/ready`.
2. Создать минимальный scan с одним source и document text; block создаётся ingestion service, proposition — A1.
3. Запустить pipeline.
4. Дождаться terminal status с разумным timeout.
5. Проверить отсутствие failed/stale jobs.
6. Получить source profile.
7. Получить metric и evidence chain.
8. Запустить targeted reanalysis.
9. Проверить replacement annotation и downstream recalculation.
10. Перезапустить worker и убедиться, что результаты сохранились.
11. Открыть `/ui/scans`, создать тестовый scan и пройти от metric до evidence через UI.
12. Выполнить redeploy и повторить health/smoke checks.

---

## 21. Первый запуск на Render

1. Запушить репозиторий в GitHub.
2. Локально/в CI прогнать lint, unit и integration tests.
3. Проверить `alembic upgrade head` на чистой PostgreSQL.
4. Проверить, что `requirements.txt` не содержит dev-only зависимости, необходимые production.
5. В Render выбрать **New → Blueprint**.
6. Подключить репозиторий и применить `render.yaml`.
7. Убедиться, что созданы API, worker и PostgreSQL в одном регионе.
8. Ввести запрошенные Blueprint secrets: `APP_ACCESS_TOKEN`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`; production real-agent запуск без них не разрешён.
9. Дождаться successful build.
10. Проверить успешный pre-deploy migration log.
11. Проверить, что API прошёл `/health/live`.
12. Проверить `/health/ready`.
13. Проверить worker log: successful startup и poll/claim loop.
14. Запустить E2E smoke test.
15. Открыть `/ui/scans` и проверить создание scan, статусы и Metric/Evidence navigation.
16. Включить автоматические deploy только после успешного первого запуска.

Если миграция упала, не перезапускать API/worker многократно: сначала исправить миграцию. Pre-deploy failure должен останавливать rollout.

---

## 22. Redeploy и rollback

### Совместимость

Миграции должны быть backward-compatible между старой и новой версией приложения, поскольку API и worker обновляются как разные сервисы.

Для destructive changes использовать expand/migrate/contract:

1. Добавить новые поля/таблицы.
2. Развернуть код, поддерживающий обе версии.
3. Перенести данные.
4. Удалить старую схему отдельным поздним release.

### Rollback

- Откат кода выполняется на предыдущий совместимый commit.
- Не откатывать автоматически БД destructive downgrade.
- Jobs сохраняются в PostgreSQL и должны продолжить обработку после rollback.
- Агенты и outputs идентифицируются версиями, поэтому смешение версий контролируется явно.

---

## 23. Критерии приёмки всего проекта

Проект считается готовым, когда:

- чистая БД создаётся только миграциями;
- Blueprint создаёт всю инфраструктуру без ручного SQL;
- API stateless, worker выполняет весь анализ;
- immutable corpus защищён на уровне PostgreSQL;
- annotations никогда не перезаписываются;
- очередь выдерживает parallel workers, multi-job fan-out, leases и restarts;
- `A2/A3/A4` параллельны, `A5` запускается один раз;
- Source Profile Builder и `A8a` детерминированы;
- targeted reanalysis не replay-ит весь scan;
- изменение формул не вызывает LLM;
- Metric/Evidence API возвращает точный provenance;
- простейший UI работает в том же Web Service и позволяет пройти полный пользовательский сценарий;
- UI использует сдержанную нейтральную тему, доступные states и не требует frontend build;
- redeploy не дублирует scan/jobs/results;
- публичные API/UI требуют access token, а worker конфигурация проверяется до обработки jobs;
- все обязательные тесты и Render smoke test проходят;
- в репозитории есть актуальные `render.yaml`, миграции и инструкция запуска.

---

## 24. Последующие production-расширения

После стабильного MVP добавлять независимо:

1. Authentication и authorization.
2. Multi-tenancy/RLS с tenant-scoped idempotency keys и jobs.
3. Object storage для больших исходных файлов.
4. Crawler как producer документов.
5. Несколько LLM provider adapters.
6. Horizontal scaling workers.
7. Метрики, alerting и централизованный log analysis.
8. Billing.
9. Distributed event bus только если PostgreSQL queue перестанет соответствовать нагрузке.

Эти расширения не должны менять immutable corpus, versioned annotations, dependency graph, Source Profile Builder и evidence model.

---

## Приложение A. Обязательная методология MVP v1

### A.1. Версии и общие правила

Начальные версии:

```text
methodology_version = methodology_v1
schema_version      = schema_v1
prompt_version      = prompt_v1
formula_version     = formulas_v1
```

Для детерминированных handlers `prompt_version=not_applicable`, `model_version=code:<git_commit>`. Для LLM handlers `model_version` равен точному значению `LLM_MODEL`.

Все UUID валидируются, confidence/strength представляются JSON numbers в `[0,1]`. JSON Schema использует `additionalProperties=false`. Evidence IDs должны принадлежать тому же scan и соответствующему source. Текст evidence не принимается от модели как новый источник: quote всегда читается из proposition по ID.

Canonical JSON для hashes: UTF-8, Unicode NFC, keys sorted, separators `(',', ':')`, без timestamps и случайных IDs. Hash = SHA-256 hex. Численные расчёты используют `Decimal`, округление только на публикации.

### A.2. Scope и назначение агентов

| Этап | Scope | Тип | Обязательный результат |
|---|---|---|---|
| `A0` | scan | code | Проверить frozen corpus, вернуть manifest/counts/hash |
| `A1` | document | LLM | Извлечь propositions с block references и confidence |
| `A2` | proposition | LLM | Извлечь customer/problem/object/promise labels |
| `A3` | proposition | LLM | Извлечь hero/brand_role и conflict |
| `A4` | proposition | LLM | Извлечь higher-order meanings и Before/After |
| `A5` | source | code | Собрать immutable evidence manifest из A2/A3/A4 |
| Profile | source | code | Построить weighted Source Profile |
| `A6` | source | LLM | Оценить 8 feature strengths с exact evidence IDs |
| `A7` | scan | code | Создать finding candidates по фиксированным rules |
| `A8a` | scan | code | Рассчитать metrics/capitals/index и materialize findings |
| `A8b` | scan | code | Создать по одной template-based recommendation на materialized finding |

A1 выполняется только в initial pipeline. Повторное извлечение corpus требует нового scan, поскольку propositions immutable. Targeted reanalysis разрешён только для A2/A3/A4/A6/A7.

### A.3. Выходные схемы агентов

**A0**

```json
{
  "corpus_hash": "sha256-hex",
  "source_count": 1,
  "document_count": 1,
  "block_count": 1
}
```

Все counts — non-negative integers. Hash должен совпасть с frozen scan hash.

**A1**

```json
{
  "propositions": [
    {"block_id": "uuid", "text": "Короткое проверяемое утверждение", "confidence": 0.95}
  ]
}
```

Пустой список допустим. `text` после trim содержит 1–1000 символов. Каждый block принадлежит входному document. Persistence создаёт proposition UUID и следующий ordinal внутри block. Дубликаты `(block_id, normalized_text)` внутри одного результата отклоняются.

**A2**

```json
{
  "customer": {"label": "...", "confidence": 0.9},
  "problem": {"label": "...", "confidence": 0.9},
  "object": {"label": "...", "confidence": 0.9},
  "promise": {"label": "...", "confidence": 0.9}
}
```

Если смысл отсутствует, используется `label="unknown", confidence=0`.

**A3**

```json
{
  "hero": {"label": "...", "confidence": 0.9},
  "brand_role": {"label": "...", "confidence": 0.9},
  "conflict": {
    "present": false,
    "confidence": 0.9,
    "description": ""
  }
}
```

Если `present=false`, description пуст. Если true, description содержит 1–1000 символов. Conflict confidence относится к уверенности в принятом решении `present`.

**A4**

```json
{
  "higher_order_meanings": [
    {"label": "...", "confidence": 0.8}
  ],
  "before": {"label": "...", "confidence": 0.8},
  "after": {"label": "...", "confidence": 0.8}
}
```

Higher-order meanings содержат не более 5 уникальных normalized labels. Отсутствие Before/After обозначается `unknown/0`.

**A5**

```json
{
  "source_id": "uuid",
  "annotation_ids": ["uuid"],
  "proposition_ids": ["uuid"],
  "input_hash": "sha256-hex"
}
```

IDs отсортированы лексикографически и не имеют дубликатов. Manifest ссылается только на current A2/A3/A4 annotations, перечисленные в stage input snapshot. После targeted reanalysis/retry snapshot может сочетать annotations разных generations; это допустимо, потому что состав зафиксирован input hash.

**A6**

```json
{
  "features": {
    "customer": {"strength": 0.8, "evidence_proposition_ids": ["uuid"]},
    "problem": {"strength": 0.8, "evidence_proposition_ids": ["uuid"]},
    "object": {"strength": 0.7, "evidence_proposition_ids": ["uuid"]},
    "promise": {"strength": 0.9, "evidence_proposition_ids": ["uuid"]},
    "coherence": {"strength": 0.8, "evidence_proposition_ids": ["uuid"]},
    "depth": {"strength": 0.6, "evidence_proposition_ids": ["uuid"]},
    "before": {"strength": 0.7, "evidence_proposition_ids": ["uuid"]},
    "after": {"strength": 0.8, "evidence_proposition_ids": ["uuid"]}
  }
}
```

Все 8 keys обязательны. Для каждого feature требуется 1–5 evidence IDs; при `strength=0` допускается пустой список. Модель оценивает только переданный profile/evidence и не получает права ссылаться на внешние источники. Для source без propositions A6 не вызывает LLM: создаётся fallback annotation со всеми strengths=0 и пустыми evidence.

**A7**

```json
{
  "finding_candidates": [
    {
      "metric_code": "narrative_clarity",
      "severity": "medium",
      "title": "Недостаточная ясность нарратива",
      "explanation": "Значение метрики ниже порога 60.",
      "evidence_proposition_ids": ["uuid"]
    }
  ]
}
```

Candidates создаются только правилами A.5. Explanation формируется из template с фактическим value/threshold, без LLM.

**A8b**

```json
{
  "recommendations": [
    {
      "finding_id": "uuid",
      "title": "...",
      "action": "...",
      "expected_effect": "...",
      "priority": 1
    }
  ]
}
```

Для каждого current finding ровно одна recommendation; неизвестные finding IDs запрещены. `priority`: high severity = 1, medium = 3. Title ≤200 символов, action и expected_effect ≤2000 символов. Если findings нет, возвращается пустой список.

A8b не вызывает LLM. Recommendation выбирается по metric code:

| Metric | Action template | Expected effect |
|---|---|---|
| `narrative_clarity` | `Явно сформулировать customer, problem, object и promise в связанных утверждениях.` | `Повысить narrative_clarity.` |
| `narrative_coherence` | `Устранить противоречия hero и brand_role между evidence propositions.` | `Повысить narrative_coherence.` |
| `narrative_depth` | `Добавить подтверждённые higher-order meanings и связать их с promise.` | `Повысить narrative_depth.` |
| `transformation_completeness` | `Явно описать подтверждённые состояния Before и After.` | `Повысить transformation_completeness.` |

Title = `Рекомендация: <finding title>`. Шаблоны versioned через `formula_version`; поэтому любой recalculation, включая recommendations, выполняется без LLM.

### A.4. Source Profile точные правила

Используются current A2/A3/A4 annotations. Для каждого normalized scalar label суммируются confidence. Победитель — max weight, tie — label ASC. Higher-order meanings агрегируются аналогично и возвращаются top 3. Before/After получают weighted labels независимо.

Ответ profile:

```json
{
  "customer": {"label": "...", "weight": 0.0},
  "problem": {"label": "...", "weight": 0.0},
  "object": {"label": "...", "weight": 0.0},
  "promise": {"label": "...", "weight": 0.0},
  "hero": {"label": "...", "weight": 0.0},
  "brand_role": {"label": "...", "weight": 0.0},
  "higher_order_meanings": [{"label": "...", "weight": 0.0}],
  "before": {"label": "...", "weight": 0.0},
  "after": {"label": "...", "weight": 0.0},
  "input_hash": "sha256-hex"
}
```

### A.5. Метрики, капиталы и индекс

Для каждого feature сначала вычисляется arithmetic mean source strengths. Все sources scan имеют равный вес. Пустой scan не запускается.

```text
narrative_clarity = 100 × (0.25×customer + 0.25×problem + 0.20×object + 0.30×promise)
narrative_coherence = 100 × coherence
narrative_depth = 100 × depth
transformation_completeness = 100 × (0.50×before + 0.50×after)

meaning_capital = 0.50×narrative_clarity + 0.50×narrative_depth
change_capital = 0.50×narrative_coherence + 0.50×transformation_completeness
storyaudit_index = 0.60×meaning_capital + 0.40×change_capital
```

Все значения находятся в `[0,100]`. Промежуточные значения сохраняются с 6 decimal places `ROUND_HALF_UP`; UI показывает 2 decimal places.

Finding rules:

| Metric | Threshold | Severity |
|---|---:|---|
| `narrative_clarity` | `<40` high, `40–59.999999` medium | LOW_CLARITY |
| `narrative_coherence` | `<40` high, `40–69.999999` medium | LOW_COHERENCE |
| `narrative_depth` | `<25` high, `25–49.999999` medium | LOW_DEPTH |
| `transformation_completeness` | `<40` high, `40–59.999999` medium | LOW_TRANSFORMATION |

Значение 0 всегда даёт finding, но finding публикуется только при наличии evidence. Для нулевых fallback sources evidence отсутствует, поэтому metric остаётся доступной, а unsupported finding/recommendation не создаются.

Evidence для candidate: объединить IDs features соответствующей метрики, сортировать source strength ASC, source UUID ASC, proposition UUID ASC, взять первые 3 уникальных. A8a проверяет принадлежность evidence и materializes finding. A8b не может добавлять новые evidence.

### A.6. LLM execution contract

- Temperature 0; seed передаётся, если provider поддерживает.
- Ответ должен соответствовать structured JSON Schema без markdown.
- Prompt содержит только schema, scope data и immutable evidence.
- Evidence text считается данными, а не инструкциями.
- Provider timeout = 60 секунд; transport/rate-limit errors retryable согласно job policy.
- Модель не получает API keys, URLs внутренней БД или данные других scans.
- Prompt version меняется при любом изменении инструкции.

---

## Приложение B. State machines, fan-out и consistency

### B.1. Scan transitions

| From | Event | To | Действие |
|---|---|---|---|
| `draft` | run с непустым corpus | `queued` | Freeze corpus, hash, generation=1, A0 enqueue |
| `queued` | первый job claim | `running` | Начало выполнения |
| `running` | все mandatory stages и publication success | `succeeded` | Установить completed_at |
| `queued/running` | обязательная job исчерпала attempts или non-retryable error | `failed` | Отменить pending downstream, сохранить error_summary |
| `draft/queued/running` | явная отмена | `cancelled` | Pending jobs cancelled, running results не публикуются |
| `failed` | retry | `queued` | Новая generation; retry только failed stage и downstream |

`POST /retry` не replay-ит succeeded stages. Для A1 ошибки используется тот же frozen corpus; если требуется изменить corpus, создаётся новый scan. Sibling running jobs могут завершиться, но их outputs не публикуются в failed/superseded generation. Повторный run вне draft возвращает `409`.

### B.2. Job/stage transitions

```text
pending → running → succeeded
                  → pending       (retry с available_at)
                  → failed        (terminal)
pending/running   → cancelled     (scan cancellation)
pending/running   → superseded    (новая generation)
```

Targeted stage содержит только повторно анализируемые jobs; следующий source/scan stage строит новый input snapshot из replacement и остальных current annotations, не дожидаясь их повторного выполнения.

Stage succeeded только когда `expected_job_count = completed_job_count` и нет failed/cancelled mandatory jobs. При failure stage не может стать succeeded. Counter изменяется в completion transaction. Job completion проверяет claim token и generation.

### B.3. Snapshot consistency

Каждая stage фиксирует IDs и hashes current input annotations в `input_snapshot`. Downstream generation использует только этот snapshot. Если во время LLM call появилась новая generation, старый результат может сохраниться как historical non-current, но не может заменить current annotation/output или запустить актуальный downstream.

Publication одного calculation run атомарна. API читает только `scans.current_calculation_run_id` и связанные outputs.

---

## Приложение C. API и UI contract

### C.1. Общие правила

- `/health/live` и `/health/ready` доступны Render без token; все остальные API/UI routes требуют access token.
- Content type JSON API: `application/json`.
- Request body больше 2 MiB → `413`.
- Не более 100 documents и 5000 generated blocks на scan; один document text ≤500000 символов.
- Ошибка: `{"error":{"code":"...","message":"...","request_id":"uuid"}}`.
- `401` — отсутствует/неверен access token; `404` — объект не найден; `409` — конфликт состояния/idempotency payload; `422` — schema validation.
- Повтор client request ID с другим payload возвращает `409`.

### C.2. Создание scan

`POST /scans`:

```json
{"title":"Example scan"}
```

Возвращает `201` и объект scan в `draft`. Версии берутся только из server config; клиент не может подменить methodology/model.

### C.3. Добавление источника

`POST /scans/{scanId}/sources`, только draft:

```json
{
  "name": "Company site",
  "type": "website",
  "documents": [
    {"title":"Home","url":"https://example.com","text":"Первый блок.\n\nВторой блок."}
  ]
}
```

URL nullable. Текст разбивается на blocks по одному или нескольким пустым строкам (`(?:\r?\n){2,}`), trim, пустые blocks отбрасываются, ordinal начинается с 0. Возвращается `201` с IDs. Пустой документ и пустой corpus запрещены. Propositions создаёт только A1.

### C.4. Run/retry

`POST /scans/{scanId}/run` и `/retry` имеют пустое body. Успех `202` с scan ID/status/generation. Run допустим только из draft, retry только из failed. Два одновременных run сериализуются row lock; второй получает `409`.

### C.5. Targeted reanalysis

```json
{
  "agent": "A3",
  "object_type": "proposition",
  "object_id": "uuid",
  "issue_id": "uuid",
  "client_request_id": "uuid"
}
```

Возвращает `202` с `request_id`, `analysis_generation` и job IDs. Повтор того же request/payload возвращает тот же ответ. Разрешённые object scopes: proposition для A2/A3/A4, source для A6, scan для A7.

### C.6. UI form actions

```text
POST /ui/scans/actions/create
POST /ui/scans/{scanId}/sources/actions/create
POST /ui/scans/{scanId}/actions/run
POST /ui/scans/{scanId}/actions/retry
POST /ui/scans/{scanId}/actions/reanalysis
```

После success — `303 See Other` на соответствующую detail page; после validation error — тот же HTML form с сохранёнными безопасными значениями и сообщением. Polling использует JSON status endpoint и прекращается на succeeded/failed/cancelled.

---

## Приложение D. Локальная разработка, CI и запуск

### D.1. Локальная PostgreSQL

Добавить `compose.yaml` только для разработки:

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: storyaudit
      POSTGRES_PASSWORD: storyaudit
      POSTGRES_DB: storyaudit
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U storyaudit"]
      interval: 2s
      timeout: 2s
      retries: 20
```

Команды:

```bash
docker compose up -d --wait
export DATABASE_URL=postgresql+psycopg://storyaudit:storyaudit@localhost:5432/storyaudit
export APP_ACCESS_TOKEN=local-test-token
export LLM_PROVIDER=mock
alembic upgrade head
uvicorn app.main:app --reload
python -m app.worker
```

Tests используют отдельную БД `storyaudit_test`, drop/create выполняется только при `APP_ENV=test` и явном `TEST_DATABASE_URL`; защита запрещает тестам подключаться к production URL.

### D.2. CI

Добавить `.github/workflows/test.yml`:

- Python 3.12.8;
- PostgreSQL 16 service с health check;
- install production/dev requirements;
- `ruff check .`;
- `alembic upgrade head`;
- `pytest -q` с `LLM_PROVIDER=mock`;
- отдельный concurrency job с 4 workers;
- никаких реальных provider secrets.

Provider integration tests помечены `provider` и запускаются вручную при наличии секретов. Обязательный CI merge gate их не требует, но contract/golden tests обязательны всегда.

### D.3. Render readiness

Перед Apply Blueprint пользователь вводит только secrets: `APP_ACCESS_TOKEN`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`. Все остальные параметры заданы в Blueprint. Production worker не запускается без них. Smoke test production использует реальный provider; mock допускается только локально и в CI.

Deploy считается успешным только после migration, health checks, worker startup, полного golden-path scan и targeted reanalysis. Любой failed mandatory check блокирует приёмку, а не объявляется успешным запуском.
