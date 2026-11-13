# Архитектура клиентского отчёта v2 (SoCode Narrative Audit v1.1)

Дата: 2026-11-12
Статус: решение принято; фазы A–F реализованы (см. `HANDOFF.md` §0.1)
Заменяет: клиентский слой `report_v1` (StoryAudit FREE/FULL)
Источники требований: `SoCode_Narrative_Audit_Methodology_v1_1.md`,
`SoCode_Basalyk_Narrative_Audit_v1_1_Methodology_Report.md`

---

## 0. Принятые решения

| # | Решение | Следствие |
|---|---|---|
| 1 | `IndexResult` / `Capital` / `formulas_v1` остаются **внутренним флагом** | клиентский отчёт их не содержит и не упоминает; включаются только при `INTERNAL_METRICS=true` |
| 2 | Отчёт v2 — **единственный** клиентский отчёт | FREE/FULL как продуктовая ось удаляется; `report_type` больше не влияет на содержание |
| 3 | Конкурентный ландшафт разрешён, **до 5 альтернатив** | `LANDSCAPE_MAX_COMPETITORS=5`, robots.txt и rate limit обязательны |
| 4 | Текст отчёта пишет **LLM-редактор** | текст — черновик под обязательным ревью аналитика; каждая секция проходит валидацию трассируемости |

Дополнительно (следствие методологии, не обсуждается):

- Никакого агрегированного индекса и никакой константы «полнота данных».
- Режим аудита (`desk` / `validated`) — главная ось, а не тариф.
- Обязательная «методологическая оговорка» на титуле.
- Человек утверждает отчёт; без утверждения отчёт имеет статус `internal_draft`.

---

## 1. Смена парадигмы: evidence-first

Было (`v1`):

```
сайт → propositions → метки A2/A3/A4 → средневзвешенные → metric → capital → index
                                      → findings по порогам метрик → recommendation
```

Стало (`v2`):

```
источник S → наблюдение F → интерпретация I → доказательство E
          → согласованность (матрица) → находка G/M/W → рекомендация R → KPI → Stage Gate
```

Правила, которые теперь обеспечивает система, а не аналитик:

1. Любой вывод в отчёте имеет `assertion_status` (`F/I/H/R/U`) и минимум один `S`-источник
   либо явный `U`.
2. Нет данных → `N/A` / `No data` / `U`, а не `0` и не средний балл.
3. `G` (противоречие) требует **двух** наблюдаемых сторон; `M` — пропуска звена;
   `W` — значимого claim без опоры. Классификатор не может понизить `G` до `M` молча.
4. Обещания причинного эффекта запрещены: `expected_effect_status` всегда `H`.
5. Scorecard не сворачивается: агрегат физически отсутствует в модели отчёта.

---

## 2. Жизненный цикл аудита

```
draft → ingesting → analyzing → registers_ready → narrative_draft
      → analyst_review → stage_gate_decided → client_ready → archived
```

`scan.status` сохраняет техническое состояние (`draft/queued/running/failed/succeeded`),
поверх него вводится `audit_phase` — продуктовое состояние отчёта.

Переходы `narrative_draft → analyst_review → client_ready` фиксируются в `review_events`
(кто, когда, что изменил, какие blockers закрыл). Отчёт с открытым `blocker`
не может получить `client_ready`.

---

## 3. Роли и стадии пайплайна

Девять ролей методологии (§18.1) + три поперечных механизма (§18.2) маппятся на стадии очереди.
Очередь, leases, fencing, generations и `stage_runs` — **переиспользуются без изменений**.

| Стадия | Роль методологии | Область (fan-out) | Выход |
|---|---|---|---|
| `A0` | Source Collector (сбор) | scan | источники, документы, снимки |
| `VERIFY` | Source Verification Layer | source | `source_registry`: дата, live-check, актуальность, доступность |
| `A1` | Proposition Extractor | block | propositions + `statements` типа `F` |
| `PRODUCT` | Proposition Extractor (карта) | scan | `products` P01…, цены, границы, аудитории |
| `LANDSCAPE` | Landscape Analyst | competitor source | `landscape_entries` C01…C05 |
| `ACTANT` | Actant & Journey Analyst | product | `actant_maps` + `actant_roles` (6 ролей) |
| `JOURNEY` | Actant & Journey Analyst | scan | `journey_steps` (8 точек) |
| `EVIDENCE` | Evidence Auditor | statement | `evidence_records` E01… (5 критериев) |
| `CONSISTENCY` | Consistency & Findings | scan | `consistency_cells` |
| `FINDINGS` | Consistency & Findings | scan | `findings_v2` G/M/W |
| `CRITIC` | Independent Critic | findings batch | `alternative_explanation`, контрпримеры, `needs_review` |
| `SCORECARD` | Scorecard & Safety Reviewer | scan | `scorecard_entries`, blockers |
| `RECOMMEND` | Recommendations Architect | scan | `recommendations_v2`, `kpis`, `roadmap_phases` |
| `NARRATIVE` | Report Editor / QA | scan | текстовые блоки секций (`report_narratives`) |
| `REPORT` | Report Editor / QA | scan | `report_snapshots` (report_v2 JSON) |

DAG (упрощённо):

```
A0 → A1 → VERIFY(source) ─┬→ EVIDENCE(statement) ─┐
        └→ PRODUCT ─→ ACTANT ─→ JOURNEY ──────────┤
LANDSCAPE(competitor) ─────────────────────────────┤
                                                    ├→ CONSISTENCY → FINDINGS → CRITIC
                                                    │                     ↓
                                                    │              SCORECARD → RECOMMEND
                                                    │                     ↓
                                                    └────────────→ NARRATIVE → REPORT
```

После `REPORT` отчёт остаётся `internal_draft` до `POST /scans/{id}/stage-gate` аналитика.

---

## 4. Модель данных

Полный DDL — `docs/report-v2-schema.md` (миграция `0002_report_v2`).

Принципы:

1. **Только аддитивные изменения.** `sources/documents/blocks/propositions` остаются
   immutable (триггеры не трогаем). Реестр источников — отдельная **изменяемая** таблица
   `source_registry`, связанная с `sources.id` (nullable, для внешних/архивных записей).
2. **Реестры — first-class таблицы**, а не поля документа отчёта.
3. **Один граф трассировки** — `trace_links(scan_id, from_type, from_id, to_type, to_id, relation)`.
   Явные FK-M2M только там, где нужен быстрый join (`recommendation_findings`).
4. **Стабильные коды** через `code_sequences` (`S`, `C`, `F`, `I`, `E`, `G`, `M`, `W`, `L`, `R`,
   `KPI`, `H`). Код выдаётся один раз и не переиспользуется при перегенерации.
5. **Снимок отчёта** — `report_snapshots(document JSONB, content_hash)`: кэш, версионирование,
   аудит изменений, будущий DOCX.
6. **Правки человека** — `review_events` (before/after JSONB), не перезапись молча.

---

## 5. Модель отчёта `report_v2`

Отчёт — проекция реестров, а не вычисление. Дженерик-модель, чтобы новые разделы
не требовали миграций:

```python
Section(id, number, title, anchor, blocks: list[Block], data_gaps: list[DataGap])
Block  = Paragraph(text, status: F|I|H|R|U|None, refs: list[str])
       | Table(caption, columns: list[Column], rows: list[list[Cell]], note)
       | Callout(variant: note|warning|blocker|methodology, title, text)
       | StatusList  # Stage Gate, Keep/Fix/Test/Stop
```

Структура (совпадает с примером Басалык, 17 разделов + приложения; §17 методологии —
его 14-раздельная сводка, маппинг приведён в таблице ниже):

| № | Раздел | Источник данных |
|---|---|---|
| 1 | Executive Summary | `report_narratives` + приоритетные findings + gate |
| 2 | Паспорт проекта, S-register, ограничения | `scans` (паспорт) + `source_registry` |
| 3 | Current Business & Proposition Map | `products` |
| 4 | Narrative Landscape & Competitor Map | `landscape_entries` |
| 5 | Declared / Experienced / Perceived | `statements` (layer) + `source_registry` |
| 6 | Greimas Actant Reconstruction | `actant_maps`, `actant_roles` |
| 7 | Narrative Customer Journey | `journey_steps` |
| 8 | Evidence Register & Claim Validation | `evidence_records` |
| 9 | Narrative Consistency Matrix | `consistency_cells` |
| 10 | Findings Register G/M/W | `findings_v2` |
| 11 | Narrative Language & Cultural Codes | `language_codes` |
| 12 | Narrative Scorecard & Confidence | `scorecard_entries` |
| 13 | Strategic Recommendations | `recommendations_v2`, `recommendation_findings` |
| 14 | Change Roadmap & Measurement | `roadmap_phases`, `kpis` |
| 15 | Stage 02 Readiness Gate | `stage_gate` |
| 16 | Гипотезы для Stage 02 | `audit_hypotheses` |
| 17 | Заключение (Keep/Fix/Test/Stop) | агрегация R + narrative |
| A | Реестр первичных ссылок | `source_registry`, `landscape_entries` |
| B | QA & limits (чек-лист) | автогенерация + `open_questions` |
| C | Методология, рамки AI, ограничения | статика + `data_gaps` |
| D | Машиночитаемое приложение | `report_snapshots.document` |

Маппинг на §17 методологии: 1→1, 2→2, 3→3, 4→5, 5→4, 6→4, 7→9, 8→7, 9→5, 10→6, 11→8,
12→11, 13→10, 14→12, 15→14, 16→10 (приложение), 17→13. Один раздел §17 может покрываться
двумя клиентскими — это допустимо, обратное (склеивание) — нет.

### Правила рендера

- HTML и PDF — из одного `report_v2`; `content_hash` — идентификатор снимка.
- Приложения-реестры печатаются в **landscape** (`@page appendix { size: A4 landscape }`).
- Оглавление с номерами страниц (заполняется после рендера через Playwright PDF-метки).
- Титул: бренд, версия отчёта, методология v1.1, дата среза, режим, **методологическая
  оговорка** — обязательный блок, не выключается флагом.
- Пустой раздел печатается как «Ограничение данных: <причина> / <последствие>», не исчезает.

---

## 6. LLM-редактор: контракт и предохранители

Вход: `RegistersBundle` (все реестры + gate + data_gaps) сериализованный в компактный JSON.
Выход: `ReportNarrative {section_id: [ParagraphDraft(text, refs, status)]}` — строгая схема.

Валидатор (детерминированный, `app/reports/validator.py`) отклоняет черновик, если:

1. `refs` пусты или ссылаются на несуществующие коды.
2. В тексте есть число/цена/дата, отсутствующая в процитированных строках реестров.
3. Встречаются запрещённые конструкции: «гарантирует», «увеличит на N %», «уникальн…»
   без основания в `W`/`C`-реестре, «подтверждено исследованием» при `audit_mode=desk`.
4. Утверждение без статуса (`F/I/H/U`) в разделе, где статус обязателен.
5. Появляется агрегированный балл/индекс там, где его нет в реестрах.

При отказе — один retry с перечнем нарушений; при повторном отказе секция собирается
шаблонно из реестров (деградация честная, не молчаливая).

Промпты версионируются (`prompt_version`) на секцию; golden fixtures — на структуру и
на отсутствие запрещённых паттернов.

---

## 7. Конкурентный ландшафт (до 5 альтернатив)

- `Source.role = competitor`, `source_metadata.selection_reason` обязателен.
- `LANDSCAPE_MAX_COMPETITORS = 5` (конфиг, валидация при добавлении).
- Перед обходом: `robots.txt`, `User-Agent`, `WEBSITE_FETCH_DELAY_SECONDS`, лимит страниц 1–3
  на конкурента (нужна страница услуги, не весь сайт).
- Извлекается только публично опубликованное: обещание, роль Helper/Opponent/Object,
  proof, цена (если опубликована), ссылка на источник.
- В отчёте всегда: «иллюстративная, не статистически репрезентативная выборка» +
  критерии включения + география.
- Ценовой сигнал формулируется как диапазон и повод для исследования, никогда как
  «рыночная медиана» и никогда как вывод «дорого».

---

## 8. Изменения API и UI

| Было | Стало |
|---|---|
| `GET /scans/{id}/report?type=free\|full` | `GET /scans/{id}/report` (единственный); `?type=` принимается и игнорируется + `Deprecation`-заголовок |
| `GET /scans/{id}/report.pdf?type=…` | `GET /scans/{id}/report.pdf` |
| — | `GET /scans/{id}/report.json` (report_v2 как контракт) |
| — | `POST /scans/{id}/report/build` (пересборка снимка) |
| — | `GET/PATCH /scans/{id}/registers/{products\|sources\|evidence\|findings\|scorecard\|recommendations\|journey\|language\|hypotheses}` |
| — | `POST /scans/{id}/stage-gate` (решение аналитика, blockers) |
| — | `POST /scans/{id}/review` (draft → review → approved) |
| UI: кнопки «FREE-отчёт»/«FULL-отчёт» | UI: «Отчёт», «Реестры», «Ревью», «Stage Gate» |

Правка реестра через PATCH создаёт `review_events` и инвалидирует `report_snapshots`
(новый снимок помечается `needs_rebuild`), но **не** пересчитывает LLM-стадии.

---

## 9. Что удаляется из клиентского слоя

| Элемент | Судьба |
|---|---|
| `IndexResult`, `Capital`, `formulas_v1` | внутренний флаг `INTERNAL_METRICS` (debug/сравнение), в отчёт не попадает |
| `DATA_COMPLETENESS = 0.55` | удалить; вместо неё — вычисляемое покрытие: `заполнено / (заполнено + N/A)` по scorecard, с явной подписью «покрытие данных, не качество бренда» |
| Разделы «Narrative Capital», «Rewriting Engine», «Предлагаемая новая нарративная архитектура», «Межканальная матрица противоречий» | не входят в Stage 01; переезжают в будущий Stage 02 |
| `free/full` гейтинг и `FREE_OBSERVATION_LIMIT` | удалить |
| `A2/A3/A4/A6` в клиентском пути | заменены ролями `PRODUCT/ACTANT/EVIDENCE/SCORECARD`; старые агенты остаются за флагом для сравнения на dev-данных |

`StoryAudit_*_Example.md` остаются как исторические примеры v1 и в тестах не используются.

---

## 10. Метрики приёмки (definition of done)

1. **Структурный тест формата:** парсер эталона Басалык извлекает 17 разделов и наборы колонок
   таблиц; сгенерированный отчёт содержит те же разделы и колонки.
2. **Запрет индекса:** в `report_v2` и JSON нет поля с агрегированным баллом.
3. **N/A-дисциплина:** при `audit_mode=desk` в разделах Experienced/Perceived не может
   появиться числовой балл (тест на уровне сборки, не шаблона).
4. **Трассируемость:** отзыв источника (`source_registry.currency=disputed` или удаление)
   помечает зависимые `statements/evidence/findings/recommendations` как `needs_review`,
   отчёт становится `needs_rebuild`.
5. **Коды стабильны:** перегенерация отчёта не меняет `S01/E01/M01/R01`.
6. **Классификация:** `G` без двух сторон отклоняется валидатором; `W` без claim тоже.
7. **QA-чеклист** из §19.1 формируется автоматически и попадает в приложение B с честными `[ ]`.
8. **PDF:** приложения-реестры в landscape, оглавление с номерами страниц, оговорка на титуле.

---

## 11. Фазы

| Фаза | Содержание | Выход | Оценка |
|---|---|---|---|
| A ✅ | миграция `0002`, реестры, коды, модель `report_v2`, рендер 17 разделов с честными `U`/`No data` | отчёт нужной формы, данные пустые | сделано 2026-11-12 |
| B ✅ | VERIFY + PRODUCT + statements + EVIDENCE + language | Proposition Map, S/E-реестр, Language Map | сделано 2026-11-12 |
| C ✅ | ACTANT + JOURNEY + CONSISTENCY + FINDINGS + CRITIC | три слоя, актанты, journey, матрица, G/M/W | сделано 2026-11-12 |
| D ✅ | SCORECARD + RECOMMEND + KPI + roadmap + GATE + NARRATIVE | клиентский текст, LLM-редактор, QA | сделано 2026-11-12 |
| E ✅ | LANDSCAPE (до 5 конкурентов) | сравнительная карта, ценовой сигнал | сделано 2026-11-12 |
| F ✅ | ревью-UI, Client Brief, open questions, PATCH-реестры, PDF cache + TOC | human-in-the-loop | сделано 2026-11-12 |
| G | Validated mode (интервью, обезличенные данные) | отдельный трек | каркас сделан: согласие, обезличивание, сессии/фрагменты, покрытие (без LLM-сопоставления слоёв) |

Порядок E после D — сознательный: формат отчёта важнее ландшафта, ландшафт заполняет
уже существующий раздел.

---

## 12. Риски

1. **LLM-редактор против запретов.** Главный риск — «красивый» текст с выдуманными связями.
   Снимается валидатором §6 и обязательным ревью; при сомнении секция деградирует в таблицу.
2. **Стоимость.** 9 ролей × fan-out по предложениям — дорого. Кэш по `scope_hash` +
   батчинг EVIDENCE + mock-провайдер для тестов остаются обязательными.
3. **Конкуренты.** Юридические и этические ограничения на цитирование; хранить только
   короткие цитаты с URL и датой, уважать robots.
4. **Immutable corpus vs изменяемые реестры.** Нельзя «править» источник; правится только
   `source_registry`/`review_events`. Тест на это есть в `tests/test_pipeline.py` — расширить.
5. **Единственный отчёт.** Удаление FREE ломает существующие ссылки/скрипты; переходный
   период: `?type=` игнорируется, старые снимки `report_v1` доступны только на чтение.
