# HANDOFF — StoryAudit

Дата: 2026-11-12
Состояние: **evidence-first отчёт `report_v2` (фазы A–F)** — единый клиентский документ
в HTML/PDF/JSON с human-in-the-loop, локально проверен end-to-end на реальном сайте.
Рабочая папка: `C:\work\narrative_audit`
Точка входа для продолжения: §0.1 (решения и статус фаз) и `docs/architecture-report-v2.md`.

---

## 0. Предыдущее обновление (2026-11-12, день) — сохранено как история

1. **Продукт сужен до анализа сайта.** На вход — только URL; текст страниц скачивается и
   извлекается сервером (`app/website.py`). Ручной ввод текста убран из UI и API.
2. **Добавлены отчёты FREE / FULL в HTML и PDF** (`app/reports/`, `report_v1`) по формату
   примеров `StoryAudit_FREE_Scan_Example.md` и `StoryAudit_FULL_Narrative_Audit_Example.md`.
   Недоступные секции честно выводятся как «Ограничения данных».
3. **Исправлены dev-скрипты.** `start-dev.ps1` пишет PID-файлы и открывает UI
   (`scripts/open-ui.ps1`); `stop-dev.ps1` убивает дерево процессов регулярками по командной
   строке, добивает осиротевшие `spawn_main` и освобождает порт 8000. До этого API после
   рестарта продолжал отдавать старый код.
4. **Тесты: 23 passed** (было 7): +10 website ingestion, +6 отчёты.

Быстрый вход: раздел 3 (запуск), 9 (ingestion сайта), 10 (отчёты), 11 (проверка).

---

## 0.1. Решение по клиентскому отчёту: report_v2 (2026-11-12, вечер)

Получены два новых документа: `SoCode_Narrative_Audit_Methodology_v1_1.md`
(методология, 719 строк) и `SoCode_Basalyk_Narrative_Audit_v1_1_Methodology_Report.md`
(эталон клиентского отчёта, 369 строк). Разрыв с текущим `report_v1` проанализирован;
принято решение перейти на **evidence-first** отчёт `report_v2`.

Принятые решения:

1. `IndexResult` / `Capital` / `formulas_v1` остаются **внутренним флагом**
   (`scans.internal_metrics`), в клиентский отчёт не попадают.
2. Отчёт v2 — **единственный** клиентский отчёт; FREE/FULL как продуктовая ось удаляется.
3. Конкурентный ландшафт разрешён, **до 5 альтернатив** (robots.txt + rate limit обязательны).
4. Текст отчёта пишет **LLM-редактор**, под обязательным ревью аналитика и с
   детерминированным валидатором трассируемости.

Документы:

- `docs/architecture-report-v2.md` — архитектура, роли пайплайна, отчёт, API/UI, фазы, риски;
- `docs/report-v2-schema.md` — DDL миграции `0002_report_v2` и контракт `report_v2`.

Главные следствия для кода: реестры (`source_registry`, `products`, `evidence_records`,
`findings_v2` G/M/W, `scorecard_entries`, `stage_gate`, …) становятся first-class таблицами;
отчёт — их проекция; агрегированный индекс и константа `DATA_COMPLETENESS` из клиентского
слоя удаляются; старые агенты `A2/A3/A4/A6` заменяются ролями `PRODUCT/ACTANT/EVIDENCE/SCORECARD`
(за флагом остаются для сравнения).

**Реализация: фазы A–F завершены (2026-11-12).**

- миграция `0002_report_v2`: 24 таблицы-реестра, 9 новых колонок `scans`,
  триггер `validate_finding`, `down_revision = 0001_initial`;
- модели реестров — `app/registers/models.py` (подключаются в `app/models.py` для metadata);
- сервисы кодов и S-реестра — `app/registers/codes.py`, `app/registers/sources.py`;
  коды `S01`, `E01`, `M01`, `R01`, `KPI01` стабильны между перегенерациями;
- модель отчёта — `app/reports/model_v2.py` (`report_v2`, 17 разделов + 4 приложения,
  `validate_document` с инвариантами);
- сборка — `app/reports/build_v2.py` (все разделы есть, незаполненные — честные `U`/`No data`/`N/A`);
- рендер — `app/reports/render_v2.py`, `app/templates/report_v2/{report,_blocks}.html`,
  `app/static/report_v2.css`; приложения печатаются в landscape через именованный `@page appendix`;
- API: единый `GET /scans/{id}/report`, `/report.pdf`, новый `/report.json`,
  `POST /report/build` (снимок с content-hash); `?type=` принимается и игнорируется
  с заголовком `Deprecation`; UI-кнопки «Нарративный аудит» / «Скачать PDF»;
- `report_v1` (`build_report`, `metrics`/`capitals`/`indices`) не удалён и остаётся
  за флагом `scans.internal_metrics` для сравнения, но из API/UI больше не отдаётся;
- тесты: **31 passed** (+8 `tests/test_report_v2.py`).

**Фаза B (реестры из корпуса):**

- новые роли в пайплайне, параллельно legacy-цепочке метрик:
  `VERIFY` (per source) → live-check, доступность/актуальность в `source_registry`;
  `PRODUCT` (scan) → продукты с ценой/длительностью/аудиторией **дословно из текста**;
  `EVIDENCE` (per source) → `statements` F + `evidence_records` E;
  `LANGUAGE` (scan) → карта языка бренда;
- `app/analysis/{common,verify,products,evidence,language}.py` — детерминированные в mock-режиме;
- предохранители: сбой live-check = ограничение, а не «страницы нет»;
  `verification=publication_only` и `allowed_conclusion="Бренд публично заявляет: …"` всегда;
  `lived` пуст и `match_status=no_data` без интервью; E-записи не дублируются при retry;
- `scan.status=succeeded` теперь ставится только когда **все** initial-jobs генерации
  завершены (`pipeline._finalize_if_ready`), а не в `A8b`; `audit_phase=registers_ready`;
- настройки: `REPORT_LIVE_CHECK`, `REPORT_LIVE_CHECK_TIMEOUT_SECONDS`,
  `REPORT_MAX_STATEMENTS`, `REPORT_MAX_PRODUCTS`; в тестах live-check выключен через
  `tests/conftest.py`, сам checker покрыт отдельным тестом с подменённым probe;
- тесты: **37 passed** (+6 `tests/test_registers.py`).

Проверено на реальном сайте (`https://www.issoft.by`): scan `succeeded`, live-check 200,
`P01`, `E01–E08`, `L01–L06`; в отчёте разделы 3, 8, 11 заполнены, 5/9/10/12–17 остаются
честными ограничениями до фаз C–D.

**Фаза C (диагностика и находки):**

- новые роли: `ACTANT` (per product), `JOURNEY`, `CONSISTENCY`, `FINDINGS`, `CRITIC` (все scan);
  цепочка: `EVIDENCE` → `LANGUAGE` + `ACTANT`×N + `JOURNEY` → `CONSISTENCY` → `FINDINGS` → `CRITIC`;
- `app/analysis/actants.py` — 6 ролей Греймаса + три уровня Object (`functional/emotional/
  transformational`) со статусами `F/I/H/U`; эмоциональный и трансформационный уровни честно
  `U` без интервью; на каждый продукт создаётся `LayerClaim` (Declared observed,
  Experienced/Perceived `unavailable`, `match_status=no_data`);
- `app/analysis/journey.py` — 8 точек пути, все `is_observed=false`; `experience`/`referral`
  всегда «нет данных» в Desk;
- `app/analysis/consistency.py` — матрица 4×4 (`subject/opponent/object/helper` ×
  `communication/service/experience/evidence`), `experience` принудительно `no_data` в Desk;
- `app/analysis/findings.py` — G/M/W: `G` требует двух несопоставимых сторон (разные цены
  **одной** услуги, а не разные услуги), `M` — пропуск звена, `W` — claim без опоры; отсутствие
  данных находкой не становится;
- `app/analysis/critic.py` — независимый проход: альтернативные объяснения, `needs_review`
  для W/historical/low-confidence; классификация не меняется молча;
- если `FINDINGS` отработал и находок нет, §10 показывает «Находок не обнаружено» как результат
  проверки, а не как «Ограничение данных»;
- миграция `0003_annotation_product` расширяет `validate_annotation` на `product` и `finding`;
- тесты: **44 passed** (+7 `tests/test_phase_c.py`).

Проверено на `https://www.issoft.by`: scan `succeeded`, 707 job без падений; заполнены §2, 3, 5–11
(актанты 8 строк, journey 8 шагов, матрица 16 ячеек, находки M01/M02/W01, критика проставила
review), §12–17 остаются ограничениями до фаз D–E. PDF — 19 страниц, приложения A–D в landscape.

**Фаза D (оценка, решения и текст):**

- новые роли: `SCORECARD` → `RECOMMEND` → `NARRATIVE` (все scan), после `CRITIC`;
- `app/analysis/scorecard.py` — 4 измерения, 15 критериев, балл 0–10 **или** `N/A` с причиной и
  `confidence` (у N/A — `null`); `experience` и «реальное узнавание» не оцениваются в Desk;
  агрегата/среднего нет ни в модели, ни в отчёте;
- `app/analysis/recommendations.py` — R привязаны к находкам, каждая с KPI
  (`baseline=unknown`) и качественными H/M/L; `expected_effect_status` навсегда `H`;
  классы `keep/fix/test/stop` (Keep — сохранение обещания, Stop — «не обещать без доказательств»);
  roadmap из 5 фаз; hypotheses H01…; Stage Gate с 6 критериями
  (`ready/conditional_ready/not_ready`), blockers и `required_validations`;
- `app/analysis/narrative.py` — LLM-редактор секций 1 и 17 + детерминированный валидатор:
  refs обязательны для F/I/H/R, все коды существуют, любое число из 3+ цифр должно быть в
  реестрах, запрещены обещания гарантированного результата, числовые прогнозы, «уникальность»,
  средний балл/NCI; при отказе — fallback-шаблон из реестров со статусом `fallback`;
- `report_narratives` (миграция `0004`) хранит проверенные абзацы; §1 и §17 собираются из них;
  пустые находки/рекомендации печатаются как «Находок не обнаружено» / «Изменений не требуется»,
  а не как ограничение данных;
- после `NARRATIVE` ставится `audit_phase=narrative_draft`; `report_status` остаётся
  `internal_draft`, пока человек не утвердит Gate (`approved_by`);
- тесты: **49 passed** (+5 `tests/test_phase_d.py`).

Проверено на `https://www.issoft.by` (scan `61f4c9ab`): 710 job без падений; заполнены все разделы
кроме §4 (ландшафт — фаза E). Scorecard 15 строк (8 баллов, 7 N/A), R01–R05
(keep/fix/fix/test/stop), KPI01–KPI05, roadmap 5 фаз, Gate `conditional_ready`,
H01–H03, нарратив §1/§17 в статусе `validated`. PDF — 21 страница, приложения A–D в landscape.

**Фаза E (конкурентный ландшафт):**

- `WebsiteSourceCreate.role` (`client|competitor`) + `selection_reason`; API и UI-форма
  «Добавить альтернативу»; критерий включения обязателен, лимит 5 альтернатив, не более 3 страниц
  на альтернативу, задержка `LANDSCAPE_FETCH_DELAY_SECONDS`;
- `app/website.py`: `robots.txt` через `urllib.robotparser` (Disallow соблюдается; недоступный
  robots.txt = разрешено), задержка между запросами (`WEBSITE_FETCH_DELAY_SECONDS`),
  `WEBSITE_RESPECT_ROBOTS`;
- `app/analysis/landscape.py`: C-реестр из публичных страниц (promise / opponent / object /
  helper / proof / price дословно, `inclusion_reason` из метаданных источника),
  `overlap_with_competitors` — пересечение ключевых слов обещания с альтернативами;
- `LANDSCAPE` (per competitor) запускается после `A1`; barrier `_maybe_scorecard` требует
  **существующие** jobs `CRITIC` — иначе scorecard стартовал до находок, и рекомендации
  строились без них (баг найден на реальном прогоне, исправлен);
- scorecard: «Относительная смысловая уникальность» считается 3/5/7 по пересечению, в
  `source_codes` — коды C; без выборки остаётся `N/A`;
- findings: W «обещание пересекается с формулировками альтернатив» (P1, если своя цена выше
  найденной), с базисом `[P.., C..]`;
- рекомендации: маркер «пересекается» → `test` с KPI «Доля верно названных отличий»;
- §4 заполняется автоматически; на реальном прогоне заполнены **все 17 разделов, data_gaps = 0**;
- тесты: **52 passed** (+3 `tests/test_landscape.py`: robots, лимит/причина, pipeline+report).

Проверено на `https://www.issoft.by` + `leverx.com` + `exadel.com` (scan `8a7e5dbe`): 1413 job
без падений, S02/S03 = competitor, C01/C02 с критериями включения, `W`-находка пересечения
отсутствует при нулевом совпадении ключевых слов (корректно), R01–R06 привязаны к находкам,
PDF — 29 страниц, приложения A–D в landscape.

**Фаза F (ревью и human-in-the-loop):**

- `app/review.py` — PATCH девяти реестров (`sources`, `products`, `evidence`, `findings`,
  `scorecard`, `recommendations`, `journey`, `language`, `hypotheses`) с whitelist полей,
  проверкой Desk/N-A инвариантов и обязательным `actor`; любая правка создаёт `review_events`
  с before/after, переводит снимки в `needs_rebuild`, не запускает LLM-стадии и не меняет corpus;
- API: `GET/PATCH /registers/*`, `GET/PUT /client-brief`, `GET/POST/PATCH /open-questions*`,
  `POST /review`, `POST /stage-gate`, `GET /review-events`; Client Brief синхронизирует Gate;
- спорный/недоступный источник помечает зависимые statements/evidence/findings/recommendations
  и scorecard `needs_review`; повторная правка после утверждения снимает approval;
- Stage Gate: `ready` с blockers → 409; только `ready` без blockers → `client_ready`,
  `conditional_ready/not_ready` остаются `internal_draft`; решение сохраняется в снимке;
- UI `/ui/scans/{id}/review`: Client Brief v2.0, открытые вопросы, Stage Gate, редакторы реестров,
  переходы ревью, пересборка снимка и журнал before/after; ссылка добавлена на страницу scan;
- миграция `0005_report_artifacts`: PDF хранится в PostgreSQL по `scan_id + content_hash`,
  ответ содержит ETag и `X-Report-Cache: HIT|MISS`; оглавление получает реальные номера страниц
  двухпроходным Chromium-рендером (21/21 раздел и приложение найдены на реальном PDF);
- исправлены `start-dev.ps1`/`stop-dev.ps1`: вложенный `$_` не находил родительский uvicorn,
  из-за чего после рестарта отвечал старый код; API и worker теперь определяются раздельно;
- тесты: **60 passed** (+7 `tests/test_review.py`, +1 TOC в `tests/test_report_v2.py`).

Проверено на scan `8a7e5dbe`: review UI 200, девять API-реестров доступны, PDF — 29 страниц,
первый запрос `MISS`, повторный `HIT` с идентичными байтами, все 21 пункт оглавления имеют
страницы. Артефакт: `logs/phase_f_report.pdf`. Финальный smoke test пройден на scan
`2870b372-b90f-45fe-883f-56bf5174441d` (2 pages, 4 metrics, targeted reanalysis).

Следующая фаза — G: Validated mode (интервью, обезличенные данные), отдельный трек.

---

## 0.6. Проверка архитектуры очереди под конкуренцией (2026-11-13)

Заявленные свойства очереди (`SKIP LOCKED` + fencing) до этого не проверялись под
параллельной нагрузкой: были тесты идемпотентности и порядка, но не конкуренции.

`tests/test_concurrency.py` — 5 тестов:

1. **4 потока-клеймера на одном скане** (`ANNOTATION_BATCH_SIZE=1`, 8 пропозиций → 32 job'а):
   ни один job не получен дважды, множество полученных == множество созданных, у всех
   `attempt_count == 1` (получен ровно один раз, без retry), все `stage_runs` сошлись
   (`completed == expected`), на каждую пару `(agent, object)` ровно одна `is_current`
   аннотация, скан `succeeded`.
2. **Чистая проверка claim'а:** 40 независимых job'ов и 4 потока, которые только клеймят;
   ноль двойных claim'ов, все 40 разобраны. Это прямая проверка `FOR UPDATE SKIP LOCKED`.
3. **Fencing:** две сессии = два worker'а. «Старый» держит job со старым `claim_token`,
   лиза истекает, job забирает «новый». `complete_job` и `heartbeat` со старым токеном
   возвращают `False` и ничего не пишут; новый владелец завершает job успешно.
4. **Восстановление лиза:** `recover_stale_jobs` возвращает job в `pending`, чистит
   `claim_token`, другой worker забирает и доводит до `succeeded`.
5. **4 реальных процесса** `python -m app.worker` дренируют скан (Linux CI; на Windows
   тест skipped): скан `succeeded`, все job'ы `succeeded`.

**Найден и исправлен реальный баг.** `claim_next_job` делал `session.get(Job, id)`, и если
job уже был в identity map сессии, возвращался **до-claim'овый** объект (`pending`, без
`claim_token`). Дальше `complete_job` сравнивал старый токен и молча возвращал `False`:
job оставался `running` до истечения лиза, работа пересчитывалась. В текущем worker'е это
не проявлялось только потому, что на каждый job создаётся новая сессия; любой цикл,
клеймящий несколько job'ов в одной сессии (батч-воркер, скрипт), попал бы в это. Исправлено:
`session.get(Job, id, populate_existing=True)`.

Тесты: **89 passed, 1 skipped** (+5 `tests/test_concurrency.py`).

## 0.5. Retention: PDF-снимки, usage и старые аннотации (2026-11-13)

До этого ничто не удалялось: `report_artifacts` хранит PDF в PostgreSQL (1–3 МБ на снимок),
`llm_usage` пишет строку на каждый запрос провайдера, а каждый reanalysis добавляет
аннотацию-предшественника. На плане `basic-256mb` это гарантированное заполнение диска.

`app/retention.py` + `scripts/prune.py`:

| Правило | Ключи | Поведение |
|---|---|---|
| PDF-снимки | `RETENTION_REPORT_ARTIFACTS_DAYS=90`, `_KEEP=3` | удаляет снимки старше 90 дней сверх 3 последних на скан; **самый свежий снимок сохраняется всегда**, даже если он единственный и старый |
| учёт токенов | `RETENTION_LLM_USAGE_DAYS=180` | строки `llm_usage` старше срока |
| аннотации | `RETENTION_ANNOTATIONS_DAYS=90` | только `is_current=false`; перед удалением обнуляется `supersedes_annotation_id`, иначе self-FK блокирует delete |

Ничего из корпуса, текущих аннотаций, находок, реестров и метаданных снимков не удаляется.
Корпус `sources → documents → blocks → propositions` неприкосновенен по определению.

Запуск:

```powershell
.venv\Scripts\python.exe scripts\prune.py                  # dry run + размеры таблиц
.venv\Scripts\python.exe scripts\prune.py --apply
.venv\Scripts\python.exe scripts\prune.py --apply --vacuum  # VACUUM FULL, освобождает диск
```

Dry run по умолчанию и всегда откатывается — его безопасно запускать на проде.
`--vacuum` идёт отдельным autocommit-соединением (VACUUM нельзя выполнять в транзакции).
В worker'е те же правила применяются при `RETENTION_ENABLED=true` раз в
`RETENTION_INTERVAL_SECONDS` и сразу после старта; ошибка retention логируется и **не**
роняет worker. В `render.yaml` worker'у выставлены прод-значения (enabled, 90/3/180/90).

Проверено на dev-БД: dry run показал размеры (`annotations 6448 kB`, `jobs 5736 kB`,
`report_artifacts 568 kB`), `--apply --vacuum` удалил подложенный 100-дневный артефакт,
оставил свежий и ужал таблицы (annotations 6448 → 5656 kB). Тесты: **85 passed**
(+5 `tests/test_retention.py`: floor по количеству, «последний снимок выживает»,
удаление usage по возрасту, очистка ссылки перед удалением аннотации, dry run
не пишет и отдаёт размеры таблиц).

## 0.4. render.yaml сверен с кодом (2026-11-13)

Blueprint обновлён под текущий код. Что было не так:

1. **API не знал про модели.** `LLM_*` были только у worker'а, а `create_scan` пишет
   `model_version` и `config_snapshot` в момент создания скана — на web-сервисе
   `LLM_PROVIDER` оставался дефолтным `mock`, и каждый прод-скан получал бы
   `model_version=mock-v1` при реальных аннотациях от настоящей модели. Теперь модели
   (`LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_MODEL_FAST`, `LLM_MODEL_SMART`)
   продублированы на оба сервиса; `LLM_API_KEY` сознательно не выдаётся web-сервису — он
   не делает вызовов провайдера.
2. **Не было настроек фаз B–F:** `WEBSITE_FETCH_DELAY_SECONDS`, `WEBSITE_RESPECT_ROBOTS`,
   `LANDSCAPE_*`, `REPORT_LIVE_CHECK*`, `REPORT_MAX_STATEMENTS/PRODUCTS`,
   `REPORT_PDF_TIMEOUT_SECONDS`, `ANNOTATION_BATCH_SIZE`, `LLM_FAST_AGENTS`,
   `LLM_SCHEMA_REPAIR_ATTEMPTS`, `LLM_TRANSPORT_RETRIES`, `LLM_REQUEST_TIMEOUT_SECONDS`,
   `LLM_PRICE_*`. Добавлены; цены — `sync: false` (при 0 эндпоинт отдаёт `cost_usd: null`).
3. **`PYTHON_VERSION: 3.12.8`** — точный патч ломается при обновлении runtime-образа
   (ровно этот класс ошибок уже стоил прогона в GitHub Actions). Стало `3.12`.
4. **Crawler живёт в API**, а не в worker'е (`POST /scans/{id}/sources` → `add_website_source`),
   поэтому `WEBSITE_*` нужны на web-сервисе — теперь они там есть, с задержкой 0.5 с.
5. **Chromium нужен только web-сервису:** PDF рендерится в API (`/report.pdf`, `/ui/...pdf`),
   worker его не рендерит. `playwright install chromium` оставлен только в build web-сервиса,
   worker собирается быстрее.
6. **`maxShutdownDelaySeconds: 60`** у worker'а: он корректно обрабатывает SIGTERM и
   завершает текущий job, не дожидаясь истечения lease.
7. **Диск БД.** PDF-снимки хранятся в PostgreSQL (`report_artifacts`, ~1–3 МБ на снимок),
   поэтому `basic-256mb` — это нижняя граница, а не оценка. В комментарии к БД и в README
   это зафиксировано: следить за `database disk usage` и поднимать план.

Код тоже подстрахован: `Settings.validate_runtime()` падает при `APP_ENV=production` +
`LLM_PROVIDER=mock` (вызывается и из `app/main.py`, и из `validate_worker()`), чтобы
неверный blueprint не превращался в тихо испорченный audit trail. Тесты: **80 passed**
(+2 `tests/test_llm.py`: прод не стартует с mock; web-сервис стартует без API-ключа,
worker — нет).

## 0.3. GitHub Actions зелёный (2026-11-13)

Репозиторий опубликован дважды: `origin` — GitLab (`a.drobyshevski/storyaudit`),
`github` — GitHub (`adrobyshevski-cyber/storyaudit`, публичный, upstream у `main`).
GitLab-пайплайн падает не по нашей вине: GraphQL отдаёт
`failureReason: "The pipeline failed due to the user not being verified."` — у аккаунта
не пройдена identity verification, CI-минуты не выдаются, пайплайн падает за 1 мс с 0 job'ов.
Контрольный прогон из трёх строк (`alpine` + `echo`) падал так же, так что `.gitlab-ci.yml`
тут не при чём. Лечится только верификацией аккаунта (или своим раннером).

GitHub Actions — **зелёный** (run `43410151237`, коммит `411ce4d`): все шаги success.

Три реальных бага CI, найденных по пути (все исправлены и проверены):

1. **`ubuntu-latest` уехал на 26.04** — `actions/setup-python@v5` не находит сборку
   `3.12.8` для этого образа, job падал на setup. Теперь `runs-on: ubuntu-24.04` +
   `python-version: "3.12"` (плавающий минор: точный патч ломается при каждом обновлении образа).
   Заодно `checkout@v5` / `setup-python@v6` (устаревание Node 20) и `wait_for_db.py` перед миграциями.
2. **`pytest` не находил пакет `app`.** Консольный скрипт `pytest` (его запускает CI) не кладёт
   CWD в `sys.path`, в отличие от `python -m pytest`; локально это было не видно, потому что
   тесты всегда шли через `-m`. Симптом — `ModuleNotFoundError` при сборе, exit code 2, меньше
   секунды. Лечение: `pythonpath = ["."]` в `[tool.pytest.ini_options]` + `python -m pytest -q`
   в обоих пайплайнах.
3. **Диагностика без токена.** Логи job'ов GitHub требуют авторизации даже для публичного
   репозитория, поэтому причина искалась через публичный API check-runs: аннотации, а для
   второго случая — временный workflow, публикующий лог как `::error::`-аннотацию.
   Временная ветка `ci-debug` и файлы диагностики удалены, `main` чистый.

## 0.2. LLM-контур для коммерции (2026-11-12, ночь)

До этого все прогоны шли на `LLM_PROVIDER=mock` (0 токенов, 0 ₽), реальный провайдер ни разу
не включался, а учёта расходов не было вообще. Закрыто три вещи.

1. **Учёт токенов и денег.** Таблица `llm_usage` (миграция `0006_llm_usage`): строка на каждый
   запрос к провайдеру — `scan_id`, `job_id`, `stage_run_id`, `agent`, `model`, `tier`,
   `purpose` (`mock|initial|repair`), `prompt/completion/cached/total_tokens`, `is_estimated`,
   `latency_ms`. Запись идёт **отдельной короткой сессией** (`app.llm_usage.DbUsageRecorder`),
   поэтому цифры выживают rollback упавшего job: неудачный скан всё равно стоит денег.
   `GET /scans/{id}/usage` отдаёт totals, разбивку по агентам и моделям, разбивку
   по `analysis_generation` и `repair_calls`; стоимость считается из
   `LLM_PRICE_INPUT_PER_MTOK` / `LLM_PRICE_OUTPUT_PER_MTOK` (по умолчанию 0 → `cost_usd: null`,
   чтобы не показывать выдуманные деньги). Если провайдер не вернул `usage`, токены
   оцениваются по длине сообщений и помечаются `is_estimated=true`.
   На странице scan появился блок «LLM usage».
2. **Маршрутизация по стадиям.** `LLM_MODEL_FAST` (объём: `A1,A2,A3,A4` — вызов на пропозицию)
   и `LLM_MODEL_SMART` (все синтезирующие стадии и редактирование §1/§17); пустые значения
   откатываются на `LLM_MODEL`, так что конфиг остаётся обратносовместимым. Список объёмных
   агентов — `LLM_FAST_AGENTS`.
3. **Ремонт схемы и транспортные повторы.** Невалидный JSON или непрошедшая схема больше не
   убивают job: ответ провайдера возвращается ему же с текстом ошибки валидации
   (`LLM_SCHEMA_REPAIR_ATTEMPTS`, по умолчанию 2 раунда). `LLMSchemaError` теперь
   `retryable=True` — после исчерпания раундов очередь даёт job ещё одну попытку (в пределах
   `jobs.max_attempts`). Транспорт: повторы на 408/409/425/429/5xx с `Retry-After` и
   экспоненциальным backoff (`LLM_TRANSPORT_RETRIES`), а 400/401/403/404/422 сразу
   `retryable=False`. Ответы в ```-фенсах или с текстом вокруг JSON разбираются.

Тесты: **78 passed** (+12 `tests/test_llm.py`: маршрутизация, fallback, запись usage, ремонт
после битого JSON и после нарушения схемы, исчерпание раундов, 429 с `Retry-After`,
non-retryable 400, оценка токенов, mock-режим, агрегаты и эндпоинт; +1 `validate`-хук;
+5 `tests/test_batching.py`: чанки, `batch_size=1`, reanalysis, guard батча, схема батча).
Проверено на живом стеке: миграция применена, smoke test прошёл, `GET /scans/{id}/usage`
вернул 35 вызовов по двум generation (в mock — нулевые токены), блок «LLM usage» рендерится.

**5. Git-репозиторий и CI (после `git init`).** Репозиторий инициализирован (ветка `main`),
всё закоммичено; `.env`, `.venv/`, `logs/` и кэши в `.gitignore`, добавлен `.gitattributes`
(`* text=auto eol=lf`, `*.ps1` — CRLF), чтобы Linux-CI не получал CRLF. Workflow
`.github/workflows/test.yml` дополнен шагом `playwright install --with-deps chromium`:
тест `test_pdf_rendering` рендерит настоящий PDF и не должен зависеть от образа раннера.
Найден и исправлен реальный баг CI: `tests/test_review.py` использовал захардкоженный
`local-test-token`, тогда как workflow задаёт `APP_ACCESS_TOKEN=test-token` — семь тестов
падали бы на GitHub. Теперь токен берётся из `get_settings().app_access_token`.
Проверка «как в CI»: чистая БД `storyaudit_ci` (`alembic upgrade head` с нуля, все шесть
миграций), запуск **без файла `.env`** и с `APP_ACCESS_TOKEN=test-token` → **78 passed**.
Локальный прогон с `.env` и dev-БД — тоже 78 passed, `ruff check .` чист.

**4. Батчинг стадий `A2/A3/A4`.** Схема (~250 токенов) больше не повторяется в каждом вызове:
`ANNOTATION_BATCH_SIZE` (по умолчанию 20) пропозиций на один job и один запрос к провайдеру.
Появились `A2/A3/A4BatchResult` с обязательным `proposition_id`; аннотации по-прежнему
сохраняются **по одной на пропозицию** с прежним payload (`customer/problem/object/promise` и т.д.),
поэтому `PROFILE`, метрики и отчёт не изменились. Ответ батча проверяется внутри repair-цикла
(`llm.generate(..., validate=...)`): пропущенный, повторённый или чужой `proposition_id` —
это раунд ремонта, а не падение job. Reanalysis остаётся одиночным (`object_type=proposition`).
`ANNOTATION_BATCH_SIZE=1` возвращает старое поведение. `config_snapshot` скана теперь хранит
`llm_provider`, `llm_model_fast`, `llm_model_smart` и `annotation_batch_size` — для трассируемости.

Замер на `https://www.issoft.by` (scan `224fb76e`, 230 пропозиций, mock): job'ов всего **56**
вместо ~710, из них `A2/A3/A4` — **36** вместо 690; аннотаций те же 690 (230 × 3),
находки 3, scorecard 15 строк, отчёт 17 разделов, coverage 9 scored / 6 `N/A`.
Ожидаемая экономия на реальном провайдере: input-токены ~−90% на этих стадиях
(250→~20 токенов схемы на пропозицию), время скана — пропорционально меньшему числу job'ов.

---

## 1. Что сделано

Реализован backend + минимальный UI по `implementation-plan.md` (v1.1) и `runtime-architecture.md`.

Покрыто:

- immutable corpus (`sources → documents → blocks → propositions`) с PostgreSQL-триггерами на `UPDATE/DELETE`;
- versioned annotations с `supersedes_annotation_id`, `is_current` и частичным unique-индексом;
- очередь `jobs` + SQL-функция `claim_next_job(worker_id)` с `FOR UPDATE SKIP LOCKED`;
- leases, heartbeat, fencing через `claim_token`, retry с backoff, recovery зависших задач;
- pipeline `A0 → A1 → [A2,A3,A4] → A5 → PROFILE → A6 → A7 → A8a → A8b`;
- fan-out по documents/propositions/sources и barriers через `stage_runs`;
- детерминированные агенты: `A0`, `A5`, `PROFILE`, `A7`, `A8a`, `A8b`;
- LLM-агенты: `A1`, `A2`, `A3`, `A4`, `A6` (mock + OpenAI-compatible adapter);
- targeted reanalysis с `reanalysis_requests`, generation и идемпотентностью;
- расчёты Metric → Capital → Index → Finding → Recommendation;
- Evidence API: Metric → Finding → Quote → Proposition → Block → Document → Source;
- JSON API + server-rendered UI (Jinja2, один CSS, минимальный JS);
- access token (Bearer для API, HTTP Basic для UI);
- Render Blueprint, CI workflow, smoke test, dev-скрипты;
- **отчёты FREE / FULL в HTML и PDF** (`app/reports/`): документ `report_v1`, шаблоны формата
  примеров `StoryAudit_*_Example.md`, рендер Chromium через Playwright, JSON-приложение,
  секция «Ограничения данных» вместо выдуманных значений;
- **ingestion только по URL сайта** (`app/website.py`): скачивание страниц, извлечение текста
  из HTML (stdlib `html.parser`, без новых зависимостей), обход same-origin ссылок в пределах
  `max_pages`, защита от SSRF (`WEBSITE_ALLOW_PRIVATE_HOSTS=false`).

---

## 2. Что физически установлено на машине

| Компонент | Путь | Версия |
|---|---|---|
| Python | `%LOCALAPPDATA%\Programs\Python\Python312` | 3.12.10 (в PATH) |
| PostgreSQL portable | `C:\work\pgsql` | 16.9 |
| Кластер | `C:\work\pgdata` | порт 5432, trust auth |
| venv проекта | `.venv` | Python 3.12.10 |
| БД | `storyaudit`, `storyaudit_test` | пользователь `storyaudit` |
| Chrome (для PDF) | `C:\Program Files\Google\Chrome\Application\chrome.exe` | используется Playwright через `channel="chrome"` |
| Playwright | `.venv` | 1.49.1 (в `requirements.txt`); браузер Chromium не скачивался |

Docker Desktop **не установлен** (требует admin + перезагрузку). Вместо него portable PostgreSQL.
`compose.yaml` оставлен в репозитории для машин с Docker — на Windows-хосте он не используется.

---

## 3. Текущее состояние процессов

Запущены и проверены:

- PostgreSQL 16.9 на `127.0.0.1:5432`;
- API `uvicorn app.main:app` на http://127.0.0.1:8000 (PID в `logs/api.pid`);
  в dev можно запускать с `--reload`, но после правок кода надёжнее перезапуск:
- worker `python -m app.worker` (PID в `logs/worker.pid`).

Данные в dev-БД на момент передачи: 20 сканов (12 `succeeded`, 8 `draft`), 3700 job,
3700 annotation, 16 sources, 19 documents, 1183 blocks, 1172 propositions.
Реестры `report_v2`: 4 products, 32 evidence, 9 findings, 30 scorecard-строк,
8 recommendations, 4 narrative-снимка. Часть `succeeded`-сканов создана до фаз B–D, поэтому
у них реестры пустые — это ожидаемо, отчёты по ним строятся как каркас с ограничениями.
Черновики — следы ручных проверок; на работу не влияют.

Логи: `logs/api.err.log`, `logs/worker.err.log`, `C:\work\pg.log`.

Управление:

```powershell
cd C:\work\narrative_audit
powershell -ExecutionPolicy Bypass -File scripts\start-dev.ps1
powershell -ExecutionPolicy Bypass -File scripts\stop-dev.ps1
```

После перезагрузки машины достаточно `start-dev.ps1`: он поднимает PostgreSQL, API и worker,
ждёт `/health/live` и в конце сам открывает UI в браузере (`scripts\open-ui.ps1`).

Открыть UI отдельно (сервер уже запущен):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\open-ui.ps1            # с токеном из .env, без диалога авторизации
powershell -ExecutionPolicy Bypass -File scripts\open-ui.ps1 -NoAuth    # обычная ссылка + диалог Basic auth
powershell -ExecutionPolicy Bypass -File scripts\open-ui.ps1 -Wait      # exit 1, если API не отвечает
```

`start-dev.ps1 -NoBrowser` — запуск без открытия браузера (CI, ssh-сессии, повторный запуск).

Запуск GUI из bash/git-bash на этой машине не работает напрямую: в `PATH` первым стоит
msys-шим (`/usr/bin/notepad` и т.п.), из-за чего `Start-Process` падает либо молча
пробрасывает команду в уже работающий скрытый инстанс Chrome. Поэтому `open-ui.ps1`
всегда резолвит `chrome.exe` по полному пути и вызывается из Windows PowerShell
(`powershell.exe -File ...`), а не из bash.

---

## 4. Проверенные результаты

| Проверка | Результат (актуальное состояние, фазы A–F) |
|---|---|
| `alembic upgrade head` (0001→0005) | успешно: реестры, `report_narratives`, `report_artifacts`, триггеры |
| `ruff check .` | без ошибок |
| `pytest -q` (с `TEST_DATABASE_URL`) | **89 passed, 1 skipped** (процессный concurrency-тест — только Linux CI) |
| `/health/live`, `/health/ready` | 200, БД ok |
| `scripts/smoke_test.py` | локальный fixture → scan `succeeded`, targeted reanalysis ok |
| реальный сайт `https://www.issoft.by` (phase D scan `61f4c9ab`) | 710 job без падений, `succeeded` |
| заполненность отчёта | все 17 разделов при наличии альтернатив; без них §4 — честное ограничение |
| scorecard | 15 строк: 8 баллов + 7 `N/A` с причиной, `experience` не оценён |
| рекомендации | R01–R05 (keep/fix/fix/test/stop), KPI01–KPI05, `baseline=unknown` |
| Stage Gate | `conditional_ready`, criteria/blockers/required_validations заполнены |
| нарратив §1/§17 | `validated`, все F/I/H/R с refs, числа сверены с реестрами |
| фаза E (issoft + 2 альтернативы) | 1413 job, S02/S03 competitor, C01/C02, все 17 разделов без gaps |
| robots.txt / лимит 5 | Disallow соблюдается; 6-я альтернатива и отсутствие причины → 422 |
| PDF | 29 страниц, приложения A–D в landscape, 21/21 пунктов TOC со страницами, cache MISS→HIT |
| Фаза F | PATCH + before/after, Brief/questions/Gate, source invalidation, UI 200 — 8 новых тестов |
| API | `report` / `.pdf` / `.json` / `POST /report/build` → 200 |
| UI `/ui/scans/{id}/report` | 200 с Basic auth; кнопки «Нарративный аудит» / «Скачать PDF» |
| `stop-dev.ps1` → `start-dev.ps1` | порт 8000 освобождается, новый код подхватывается |
| pipeline + reanalysis в тесте | 2 версии A3-аннотации, ровно 1 current |
| стабильность снимка | `content_hash` не меняется при перегенерации без правок |

---

## 5. Карта кода

| Файл | Ответственность |
|---|---|
| `app/main.py` | FastAPI app, middleware, health, error handlers |
| `app/api.py` | JSON API, auth dependency, metric/evidence view |
| `app/ui.py`, `app/templates/`, `app/static/` | server-rendered UI |
| `app/models.py` | SQLAlchemy-модели всех сущностей |
| `app/queue.py` | enqueue, claim, complete, fail, heartbeat, recovery |
| `app/pipeline.py` | fan-out, зависимости, targeted reanalysis |
| `app/agents.py` | исполнение `A0–A8b`, публикация outputs |
| `app/calculations.py` | weighted mode, формулы, finding rules |
| `app/scans.py` | scan lifecycle, corpus ingestion, retry |
| `app/website.py` | ingestion сайта: HTML → текст, same-origin crawl, SSRF-guard, robots.txt, rate limit |
| `app/reports/model.py` | `ReportDocument` (report_v1): секции отчёта, `DataGap`, hash снимка |
| `app/reports/build.py` | БД → отчёт: индикаторы, корпус, роли, gaps, findings, план, ограничения |
| `app/reports/render.py` | HTML (Jinja2) и PDF (Playwright/Chromium), inline CSS, proactor-loop на Windows |
| `app/templates/report/` | `report.html` + `partials/{gaps,observations}.html`, `app/static/report.css` |
| `app/reports/build_v2.py` | сборка `report_v2` из реестров (17 разделов + приложения, честные gaps) |
| `app/reports/model_v2.py` | `report_v2`: Section/Block, coverage, `validate_document`, `document_hash` |
| `app/reports/render_v2.py` | HTML/PDF для `report_v2`, landscape-приложения, content-hash в шапке |
| `app/templates/report_v2/` | `report.html` + `_blocks.html` (paragraph/table/callout/kv/status) |
| `app/static/report_v2.css` | вёрстка отчёта, `@page` + `@page appendix` (landscape) |
| `app/registers/models.py` | 24 таблицы-реестра (S/E/G/M/W/R/KPI/Gate/снимки и т.д.) |
| `app/registers/codes.py`, `sources.py` | стабильные коды и S-реестр поверх immutable corpus |
| `app/analysis/verify.py` | live-check источника; сбой становится ограничением, не вердиктом |
| `app/analysis/products.py` | `products` из корпуса: цена/длительность/аудитория дословно |
| `app/analysis/evidence.py` | отбор claim'ов, F-утверждения, E-реестр, trace_links |
| `app/analysis/language.py` | карта языка бренда; lived-язык без интервью остаётся пустым |
| `app/analysis/actants.py` | 6 ролей Греймаса + 3 уровня Object, статусы F/I/H/U, LayerClaim |
| `app/analysis/journey.py` | 8 точек пути, `is_observed=false`, опыт/реферал — «нет данных» |
| `app/analysis/consistency.py` | матрица 4×4; `no_data` не превращается в конфликт |
| `app/analysis/findings.py` | G/M/W: G требует двух сторон, W — claim без опоры |
| `app/analysis/critic.py` | независимый проход, альтернативы, `needs_review` |
| `app/analysis/scorecard.py` | 4 измерения × критерии, 0–10/N/A + confidence, без агрегата |
| `app/analysis/recommendations.py` | R ↔ finding, KPI, roadmap, hypotheses, Stage Gate |
| `app/analysis/narrative.py` | LLM-редактор §1/§17 + детерминированный валидатор и fallback |
| `app/analysis/landscape.py` | C-реестр, `overlap_with_competitors`, критерии включения |
| `app/review.py` | human edits, Client Brief/questions/review/Gate, before/after, invalidation |
| `app/reports/artifacts.py` | PostgreSQL-кэш PDF по content-hash, безопасен при гонке workers |
| `alembic/versions/0005_report_artifacts.py` | таблица PDF-артефактов |
| `app/templates/review.html` | ревью-UI, формы Brief/questions/Gate и редакторы реестров |
| `alembic/versions/0003_annotation_product.py` | `product`/`finding` в validate_annotation |
| `alembic/versions/0004_report_narratives.py` | таблица `report_narratives` |
| `alembic/versions/0002_report_v2.py` | аддитивная миграция реестров и колонок `scans` |
| `app/llm.py` | mock и OpenAI-compatible провайдеры; маршрутизация fast/smart, ремонт схемы, повторы, разбор JSON |
| `app/llm_usage.py` | запись `llm_usage`, агрегаты по агенту/модели/generation, оценка стоимости |
| `alembic/versions/0006_llm_usage.py` | таблица учёта токенов и стоимости |
| `app/schemas.py` | Pydantic-схемы API и агентов |
| `app/config.py` | настройки и валидация worker |
| `alembic/versions/0001_initial.py` | схема, триггеры, `claim_next_job` |
| `methodology/` | formulas_v1, golden fixture, JSON Schema A2 |
| `scripts/start-dev.ps1`, `scripts/stop-dev.ps1` | запуск/остановка стека, PID-файлы, освобождение порта 8000 |
| `scripts/open-ui.ps1` | открытие UI в Chrome с токеном из `.env`, ожидание `/health/live` |
| `scripts/smoke_test.py` | end-to-end проверка: сайт → scan → metrics → reanalysis |
| `StoryAudit_*_Example.md` | эталонные примеры формата отчёта FREE / FULL (в корне проекта) |
| `docs/architecture-report-v2.md` | архитектура `report_v2`: evidence-first, роли пайплайна, API/UI, фазы |
| `docs/report-v2-schema.md` | DDL миграции `0002_report_v2`, контракт `report_v2`, инварианты валидатора |
| `SoCode_*_v1_1*.md` | новая методология SoCode v1.1 и эталон клиентского отчёта (требования к `report_v2`) |

---

## 6. Ключевые проектные решения

1. **Очередь в PostgreSQL**, без Redis. Задачи забираются через `claim_next_job` с `SKIP LOCKED`.
2. **Fencing**: `claim_token` + `analysis_generation` в каждом UPDATE — старый worker не может завершить чужую задачу.
3. **Idempotency**: initial `{scan}:{agent}:{scopeHash}:{promptVersion}`, reanalysis — с `generation`, что позволяет повторно анализировать тот же issue по новому запросу.
4. **Публикация outputs атомарна**: сначала материализуются non-current результаты, затем один переключатель `scans.current_calculation_run_id`.
5. **A8b детерминирован** (шаблоны по metric code), чтобы recalculation никогда не вызывал LLM.
6. **Immutable corpus**: повторное извлечение propositions = новый scan, не мутация.
7. **UI в том же Web Service**: никакого Node build, отдельного Static Site или второго Render-сервиса.
8. **Auth минимальная**: один `APP_ACCESS_TOKEN` (Bearer/Basic), health-эндпоинты публичные.
9. **Отчёт = один документ, три вывода.** `ReportDocument` (`report_v1`) рендерится в HTML и PDF и
   отдаётся как JSON-приложение; `content_hash` документа — идентификатор снимка отчёта.
10. **Честная неполнота.** Разделы, которых нет в текущей методологии (NCI v2, Hero Ratio,
    Ego Index, Кэмпбелл, SB7, межканальная матрица, rewriting), выводятся как `DataGap`
    с причиной и последствием — цифры не выдумываются, в отличие от demo-примеров.
11. **CSS встраивается перед PDF-рендером**: `page.set_content()` не имеет base URL, `<link>`
    не резолвится, Chromium отрендерил бы Times вместо Segoe UI.
12. **Chromium выбирается с фолбэком**: сначала локальный Chrome (`PLAYWRIGHT_CHANNEL=chrome`),
    затем встроенный (`playwright install chromium`).

---

## 7. Известные ограничения (важно для следующего шага)

> Раздел описывает текущую реализацию (`report_v1`). Пункты подраздела «Отчёты»
> замещаются решением `report_v2` (см. §0.1 и `docs/architecture-report-v2.md`).

### Функциональные

- **MVP анализирует только сайты.** На вход — URL; текст страниц извлекается сервером.
  Загрузки файлов и вставки текста в продукте нет (`app.scans.add_source` остался внутренним
  helper'ом для тестов). См. раздел «Только анализ сайта» ниже.
- **Реальный LLM не проверялся.** Все прогоны выполнены на `LLM_PROVIDER=mock`. Adapter OpenAI-compatible написан, но против живого провайдера не тестировался; quality промптов не оценивалась.
- **A6 в mock-режиме эвристический**: strengths считаются из profile и conflict-ов, а не моделью. Это осознанно только для dev/smoke.
- **LLM mock возвращает шаблонные labels** (`customer`, `product`, …), поэтому метрики на dev-данных не отражают реальное качество анализа.
- **Один worker.** Горизонтальное масштабирование поддержано схемой, но не проверено под нагрузкой.
- **A8b создаёт одну recommendation на finding**; richer recommendation logic не реализован.
- **Нет pagination** в list-эндпоинтах (стоят жёсткие limit).
- **Retry не поддерживает failed reanalysis**: для повторного reanalysis нужен новый `client_request_id` (сделано намеренно).

### Отчёты

- **PDF рендерится синхронно в HTTP-запросе** (1.5–3 с, Chromium запускается на каждый запрос);
  кэша артефактов и выноса в `jobs` пока нет.
- **Оглавление без номеров страниц** — разделы в PDF нумеруются подвалом «стр. N / M».
- **«Полнота данных 55%»** — константа `DATA_COMPLETENESS` в `app/reports/build.py`.
- **FREE/FULL гейтинг частичный**: различаются набор секций и лимит наблюдений (FREE ≤3),
  но тарифной модели и проверки числа каналов нет.
- **Отчёт доступен только для `succeeded` scan**; draft/queued/failed дают 409.
- **Вёрстка рассчитана на кириллицу и A4**; других локалей и форматов (Letter) нет.

### Инфраструктурные

- **Render не проверен фактически.** `render.yaml` подготовлен, но Blueprint не применялся.
  Для PDF в `buildCommand` добавлен `playwright install chromium`; наличие системных
  библиотек Chromium на Render native-runtime не проверялось.
- **Docker Desktop отсутствует** — локально используется portable PostgreSQL.
- **Git-репозиторий не инициализирован** (`git init` не выполнялся). Для Render это первый обязательный шаг.
- **Тесты конкуренции ограничены**: есть проверки идемпотентности и порядка, но нет нагрузочного теста с 4+ параллельными worker-процессами.
- **Миграция одна.** Downgrade корректен только для пустой БД.
- **`alembic upgrade head` в тестах** выполняется через `tests/conftest.py` при заданном `TEST_DATABASE_URL`.

### Безопасность

- Нет пользователей, ролей, tenant isolation, RLS.
- Access token общий на весь сервис; ротация не реализована.
- Rate limiting отсутствует: один токен может поставить много LLM-задач.

---

## 8. Следующие шаги (по приоритету)

**Основная линия — `report_v2`** (см. §0.1, `docs/architecture-report-v2.md` §11):
A) ✅ миграция `0002`, реестры, коды, модель `report_v2`, рендер 17 разделов с `U`/`No data` →
B) ✅ `VERIFY`/`PRODUCT`/`EVIDENCE`/язык → C) ✅ `ACTANT`/`JOURNEY`/`CONSISTENCY`/`G-M-W`/`CRITIC` →
D) ✅ `SCORECARD`/`RECOMMEND`/`KPI`/`GATE`/LLM-редактор → E) ✅ ландшафт до 5 конкурентов →
F) ✅ ревью-UI и human-in-the-loop → G) ⏭ Validated mode (интервью, обезличенные данные).

Инфраструктурная очередь (можно параллельно, не блокирует отчёт):

1. ✅ `git init` + коммиты сделаны (ветка `main`). Заказчик использует **GitLab**, поэтому
   добавлен `.gitlab-ci.yml` (postgres:16 + python:3.12-slim, `scripts/wait_for_db.py`,
   миграции, ruff, Chromium, pytest) — эквивалент GitHub-workflow, оба зелёные.
   Осталось: создать пустой проект в GitLab, `git remote add origin ...` и `git push -u origin main`
   (нужен ваш аккаунт/PAT). Render поддерживает GitLab-репозитории, `render.yaml` не меняется.
2. Прогнать реального провайдера: выставить `LLM_PROVIDER=openai_compatible`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, выполнить smoke test, проверить валидацию схем и поведение при невалидном JSON.
3. Настроить качество `A6`: заменить mock-эвристику на реальный prompt, добавить golden fixtures с ожидаемыми strengths.
4. Применить `render.yaml` в Render, ввести secrets, выполнить production smoke и redeploy-тест.
5. Добавить тест конкуренции: 4 worker-процесса на одной БД, проверка отсутствия double-claim.
6. Добавить pagination и базовый rate limiting на запуск scan/reanalysis.
7. Рассмотреть retention/cleanup для старых generations аннотаций и runs.
8. Ingestion сайта вынести в очередь (`jobs`), добавить `robots.txt`, rate limiting к целевому
   сайту и опциональный headless-рендеринг для SPA.

Отчёты — прежний план `formulas_v2`/24 метрики **отменён** решением `report_v2`: методология v1.1
сознательно отказывается от композитов и индекса. Вместо него — фазы A–F выше.
Внутри `report_v2` сохраняются только эти задачи из старого списка:

9. ✅ Кэш PDF в PostgreSQL и номера страниц в оглавлении сделаны в F; вынос рендера в очередь остаётся инфраструктурной задачей.
10. Мультиканальность — в рамках реестра источников (`source_registry.role`), без отдельного
    Contradiction Engine: противоречия живут в `consistency_cells` и `findings_v2`.
11. `ARCHITECTURE`/`REWRITE`/`PLAN` (Brand Myth, Message House, 3 варианта переписывания) —
    это Stage 02, вне клиентского отчёта Stage 01.

---

## 9. Только анализ сайта (MVP)

Поток: `POST /scans` → `POST /scans/{id}/sources {"url": "...", "max_pages": 5}` → `POST /scans/{id}/run`.
В UI: форма «Новый анализ сайта» на `/ui/scans` (название + URL + число страниц).

Что делает `app/website.py`:

1. `normalize_site_url` — добавляет `https://` при отсутствии схемы, отбрасывает query/fragment,
   разрешает только `http`/`https`, запрещает credentials в URL.
2. SSRF-guard: резолвит хост и отклоняет private/loopback/link-local/reserved адреса, если
   `WEBSITE_ALLOW_PRIVATE_HOSTS != true`. Проверяется и финальный URL после редиректов.
3. Загружает страницы (httpx, лимит 2 МБ на страницу, таймаут `WEBSITE_FETCH_TIMEOUT_SECONDS`),
   принимает только `text/html`/`application/xhtml+xml`, ходит только по same-origin ссылкам,
   не более `max_pages` (максимум 20).
4. `parse_html` — stdlib `HTMLParser`: выкидывает `script/style/nav/footer/header/aside/form/...`,
   режет текст по блочным тегам, декодирует entities, отбрасывает пустые/мусорные абзацы.
5. `add_website_source` создаёт `Source(type="website", external_id=<host>, source_metadata={entry_url, pages})`,
   по `Document` на страницу и `Block` на абзац (тот же сплиттер, что и раньше).

Ограничения, о которых надо знать:

- **Нет JS-рендеринга** (нет headless-браузера): SPA с клиентским текстом дадут пустой результат
  и `422 «Не удалось получить текст с сайта»`.
- **`robots.txt` и rate limit реализованы** для ingestion (включая альтернативы): Disallow
  соблюдается, между запросами выдерживается `WEBSITE_FETCH_DELAY_SECONDS` /
  `LANDSCAPE_FETCH_DELAY_SECONDS`. Кэша ответов и повторного использования снимков нет.
- Загрузка синхронная внутри HTTP-запроса API (до `max_pages × timeout`): при больших сайтах
  запрос долгий; в следующей версии ingestion стоит вынести в очередь (`jobs`).
- Текст не дедуплицируется между страницами; повторы (меню, футер) частично отфильтрованы
  по тегам, но не полностью.
- Лимиты корпуса прежние: ≤100 documents и ≤5000 blocks на scan.

## 10. Отчёты FREE/FULL и PDF (report_v1 — заменён на report_v2)

> Актуальный отчёт — `report_v2` (§0.1). Раздел сохранён как описание `report_v1`,
> который больше не отдаётся через API/UI и остаётся только за флагом `internal_metrics`.

Поток: `build_report()` читает текущий calculation run → `ReportDocument` → HTML (Jinja2) → PDF (Chromium).

Эндпоинты:

```text
GET /scans/{id}/report?type=free|full      HTML (API, Bearer/Basic)
GET /scans/{id}/report.pdf?type=free|full  PDF
GET /ui/scans/{id}/report?type=...         HTML-превью
GET /ui/scans/{id}/report.pdf?type=...     PDF из UI
```

В UI на странице `succeeded` scan — кнопки «FREE-отчёт», «FULL-отчёт», «Скачать PDF».

Ключевые решения:

1. **Один документ — три вывода.** PDF, HTML и JSON-приложение строятся из одного `ReportDocument`,
   его `content_hash` — идентификатор снимка отчёта.
2. **Честная неполнота.** Чего методология ещё не считает (NCI v2, Hero Ratio, Ego Index,
   Кэмпбелл, SB7, межканальная матрица, Rewriting Engine), выводится в «Ограничения данных»
   с причиной и последствием — вместо синтетических чисел, как в примерах-демо.
3. **CSS встраивается в HTML перед рендером.** `page.set_content()` не имеет base URL, поэтому
   `<link rel="stylesheet">` не резолвится и Chromium отрендерил бы Times вместо Segoe UI.
4. **Proactor loop на Windows.** uvicorn переключает политику на selector, где
   `asyncio.create_subprocess_exec` не реализован; `render_pdf` временно ставит
   `WindowsProactorEventLoopPolicy` и возвращает прежнюю.
5. **Chromium: сначала локальный Chrome** (`PLAYWRIGHT_CHANNEL=chrome`), иначе — встроенный
   (`playwright install chromium`). На Render это добавлено в `buildCommand`.

Ограничения текущей версии отчёта:

- рендер синхронный, внутри HTTP-запроса (1.5–3 с на документ); кэш PDF и вынос в `jobs` — фаза 6;
- нет пагинации оглавления с номерами страниц (только список разделов);
- «Полнота данных 55%» — константа `DATA_COMPLETENESS`; должна считаться по факту заполнения секций;
- REPORT_TYPE учитывается в шапке, наборе секций и лимите наблюдений (FREE ≤3), но гейтинг
  по каналам/тарифу появится вместе с мультиканальным корпусом.

Прежний план `formulas_v2`/24 метрики **отменён** (см. §0.1): методология v1.1 отказывается
от композитов и индекса. Актуальный план фаз A–F — §8 и `docs/architecture-report-v2.md` §11;
из старого списка остаются только кэш PDF и нумерация страниц в оглавлении (фаза F).

## 11. Как быстро проверить, что всё живо

```powershell
cd C:\work\narrative_audit
powershell -ExecutionPolicy Bypass -File scripts\start-dev.ps1
.venv\Scripts\python.exe scripts\smoke_test.py
```

Ожидаемый вывод: `Smoke test passed: scan=..., site=..., pages=2, metrics=4, reanalysis=...`

Проверить отчёт:

```powershell
$sid = (Invoke-RestMethod -Headers @{Authorization="Bearer local-test-token"} http://127.0.0.1:8000/scans)[0].id
Start-Process "http://audit:local-test-token@127.0.0.1:8000/ui/scans/$sid/report.pdf"
# HTML:  http://127.0.0.1:8000/ui/scans/$sid/report
# JSON:  http://127.0.0.1:8000/scans/$sid/report.json  (заголовок Authorization: Bearer ...)
```

Полный отчёт имеет смысл смотреть на скане, прошедшем фазы A–F; после pipeline откройте
«Реестры и ревью», заполните Brief/questions и зафиксируйте Stage Gate. `?type=free|full` больше не влияет
на содержание: параметр принимается и игнорируется с заголовком `Deprecation`.

Полная проверка тестов:

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://storyaudit:storyaudit@localhost:5432/storyaudit_test"
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q
```

---

## 12. Важные детали для принимающего

- `.env` создан из `.env.example` и **не коммитится**; production-секреты вводятся в Render.
- `APP_ENV=production` + `LLM_PROVIDER=mock` намеренно падает при старте worker.
- Health-check Render смотрит на `/health/live`, а не `/health/ready` — чтобы недоступность БД не вызывала цикл рестартов.
- Миграции выполняются только через `preDeployCommand` Web Service, не в `startCommand`.
- При ручном `pg_ctl` из bash процесс умирает вместе с родительской оболочкой; для этого и сделаны `start-dev.ps1` через `Start-Process`.
- `uvicorn --reload` порождает детей через базовый интерпретатор Python, поэтому `stop-dev.ps1`
  ищет процессы по командной строке и освобождает порт 8000, а не только по пути `.venv`. Раньше
  осиротевший reload-потомок держал порт и API продолжал отдавать старый код после рестарта.
- `WEBSITE_ALLOW_PRIVATE_HOSTS=true` только в локальном `.env` (нужно для fixture-сайта smoke-теста);
  в Render — `false`.
- Новые ключи фазы E (есть в `.env.example`): `WEBSITE_FETCH_DELAY_SECONDS`,
  `WEBSITE_RESPECT_ROBOTS`, `LANDSCAPE_MAX_COMPETITORS=5`, `LANDSCAPE_MAX_PAGES=3`,
  `LANDSCAPE_FETCH_DELAY_SECONDS=0.5`.
- Пароль `local-test-token` — только локальный; в Render он заменяется секретом.
- Новые ключи `.env` (есть в `.env.example`): `WEBSITE_MAX_PAGES`, `WEBSITE_FETCH_TIMEOUT_SECONDS`,
  `WEBSITE_ALLOW_PRIVATE_HOSTS`; PDF-таймаут — `report_pdf_timeout_seconds` (по умолчанию 60).
- Для PDF нужен Chromium: локально используется установленный Chrome
  (`PLAYWRIGHT_CHANNEL=chrome`), иначе `playwright install chromium`.
- В dev-БД лежат 8 черновиков scan — следы проверок UI; их можно удалить, отчёты они не затрагивают.
