# report_v2 — схема данных и контракт отчёта

Дата: 2026-11-12
Миграции: `0002_report_v2.py` (реестры) и `0003_annotation_product.py`
(`product`/`finding` в `validate_annotation`)
Основание: `docs/architecture-report-v2.md`

Изменения только аддитивные: таблицы `sources`, `documents`, `blocks`, `propositions`
остаются immutable (триггеры `reject_corpus_mutation` не трогаем).

---

## 1. Расширение `scans` (паспорт проекта)

```sql
ALTER TABLE scans
  ADD COLUMN brand                 text,
  ADD COLUMN audit_mode            text NOT NULL DEFAULT 'desk'
      CHECK (audit_mode IN ('desk','validated')),
  ADD COLUMN snapshot_date         date,
  ADD COLUMN geography             text,
  ADD COLUMN category_status       text DEFAULT 'unknown'
      CHECK (category_status IN ('confirmed','declared','unknown')),
  ADD COLUMN missing_materials     jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN client_brief          jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN audit_phase           text NOT NULL DEFAULT 'draft'
      CHECK (audit_phase IN ('draft','ingesting','analyzing','registers_ready',
                             'narrative_draft','analyst_review','stage_gate_decided',
                             'client_ready','archived')),
  ADD COLUMN internal_metrics      boolean NOT NULL DEFAULT false;

-- методология больше не v1
ALTER TABLE scans ALTER COLUMN methodology_version SET DEFAULT 'socode_v1.1';
```

`internal_metrics` — единственный переключатель для `IndexResult`/`Capital`/`formulas_v1`.

---

## 2. Коды

```sql
CREATE TABLE code_sequences (
  scan_id    uuid NOT NULL REFERENCES scans(id),
  prefix     text NOT NULL,          -- S, C, F, I, E, G, M, W, L, R, KPI, H
  next_value integer NOT NULL DEFAULT 1,
  PRIMARY KEY (scan_id, prefix)
);
```

Выдача кода — `SELECT ... FOR UPDATE` + `UPDATE next_value = next_value + 1`.
Коды не переиспользуются: удалённая находка оставляет дырку в нумерации, это допустимо.

---

## 3. S-register (реестр источников)

```sql
CREATE TABLE source_registry (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id        uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  code           text NOT NULL,                      -- S01
  source_id      uuid REFERENCES sources(id),         -- NULL для внешних/архивных записей
  url            text,
  role           text NOT NULL DEFAULT 'client'
      CHECK (role IN ('client','competitor','review','archive','internal','methodology')),
  provenance     text NOT NULL DEFAULT 'primary'
      CHECK (provenance IN ('primary','secondary','internal','historical')),
  availability   text NOT NULL DEFAULT 'full'
      CHECK (availability IN ('full','partial','unavailable')),
  currency       text NOT NULL DEFAULT 'current'
      CHECK (currency IN ('current','historical','disputed','unknown')),
  captured_at    timestamptz NOT NULL DEFAULT now(),
  live_checked_at timestamptz,
  http_status    integer,
  content_hash   text,
  confirms       text,                               -- что подтверждает
  limitations    jsonb NOT NULL DEFAULT '[]'::jsonb,
  metadata       jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scan_id, code)
);
CREATE INDEX source_registry_role ON source_registry(scan_id, role);
```

`source_registry` изменяема (в отличие от `sources`): актуальность и live-check обновляются
повторно, история правок — в `review_events`.

---

## 4. Продукты и ландшафт

```sql
CREATE TABLE products (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id        uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  code           text NOT NULL,                      -- P01
  name           text NOT NULL,
  audience       text NOT NULL DEFAULT 'unknown',
  trigger_situation text,
  promise        text,
  format         text,
  price          text,                               -- текст: "18 000 ₽ / 60 мин"
  procedure      text,
  proof          text,
  boundary       text,                               -- что не гарантируется
  status         text NOT NULL DEFAULT 'current'
      CHECK (status IN ('current','secondary','legacy','future','unknown')),
  is_primary     boolean NOT NULL DEFAULT false,
  source_codes   jsonb NOT NULL DEFAULT '[]'::jsonb, -- ["S01","S02"]
  metadata       jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scan_id, code)
);

CREATE TABLE landscape_entries (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id           uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  code              text NOT NULL,                   -- C01..C05
  name              text NOT NULL,
  url               text NOT NULL,
  source_code       text,                            -- S-код страницы конкурента
  audience          text,
  opponent          text,
  object            text,
  helper_role       text,
  promise           text,
  proof             text,
  price             text,
  inclusion_reason  text NOT NULL,                   -- критерий включения (обязателен)
  geographic_scope  text,
  territory_notes   text,
  fetched_at        timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scan_id, code)
);
```

Ограничение «до 5 конкурентов» — на уровне приложения (`LANDSCAPE_MAX_COMPETITORS=5`)
и проверкой в `POST /scans/{id}/sources` при `role=competitor`.

---

## 5. Утверждения (F / I / H / U)

```sql
CREATE TABLE statements (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id       uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  code          text NOT NULL,                       -- F01 / I01 / H01? (см. ниже)
  kind          text NOT NULL CHECK (kind IN ('F','I','H','R','U')),
  sub_status    text NOT NULL DEFAULT 'current'
      CHECK (sub_status IN ('current','historical','disputed','unavailable')),
  layer         text NOT NULL DEFAULT 'na'
      CHECK (layer IN ('declared','experienced','perceived','na')),
  text          text NOT NULL,
  product_code  text,
  source_codes  jsonb NOT NULL DEFAULT '[]'::jsonb,
  confidence    text NOT NULL DEFAULT 'medium'
      CHECK (confidence IN ('high','medium','low','unknown')),
  needs_review  boolean NOT NULL DEFAULT false,
  review_status text NOT NULL DEFAULT 'machine'
      CHECK (review_status IN ('machine','analyst_edited','approved')),
  metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scan_id, code)
);
```

Отдельный реестр `audit_hypotheses` (ниже) хранит H уровня Stage 02; `statements.kind='H'`
используется для гипотез внутри диагностики (например, скрытый Sender).

---

## 6. E-register

```sql
CREATE TABLE evidence_records (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id           uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  code              text NOT NULL,                   -- E01
  statement_id      uuid REFERENCES statements(id) ON DELETE SET NULL,
  claim             text NOT NULL,
  source_codes      jsonb NOT NULL DEFAULT '[]'::jsonb,
  provenance        text NOT NULL
      CHECK (provenance IN ('primary','original_document','retelling','review','unknown')),
  verification      text NOT NULL
      CHECK (verification IN ('independently_confirmed','publication_only',
                              'disputed','unknown')),
  relevance         text NOT NULL CHECK (relevance IN ('high','medium','low')),
  freshness         text NOT NULL CHECK (freshness IN ('current','outdated','unknown')),
  limitations       jsonb NOT NULL DEFAULT '[]'::jsonb,
  allowed_conclusion text NOT NULL,                  -- "Бренд заявляет стаж"
  confidence        text NOT NULL DEFAULT 'medium'
      CHECK (confidence IN ('high','medium','low')),
  needs_review      boolean NOT NULL DEFAULT false,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scan_id, code)
);
```

Проверка при вставке (приложение + DB-триггер): `verification='independently_confirmed'`
требует ≥1 источника с `provenance IN ('primary','original_document')` и `role != 'review'`.

---

## 7. Три слоя

```sql
CREATE TABLE layer_claims (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id       uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  code          text NOT NULL,                       -- T01
  promise       text NOT NULL,
  declared      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {text, source_codes}
  experienced   jsonb NOT NULL DEFAULT '{"status":"unavailable"}'::jsonb,
  perceived     jsonb NOT NULL DEFAULT '{"status":"unavailable"}'::jsonb,
  match_status  text NOT NULL DEFAULT 'no_data'
      CHECK (match_status IN ('confirmed','partial','conflict','no_data')),
  unknown_note  text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scan_id, code)
);
```

При `scans.audit_mode='desk'` поля `experienced`/`perceived` не могут получить
`status='observed'` — проверка в сборке отчёта.

---

## 8. Актанты, journey, согласованность

```sql
CREATE TABLE actant_maps (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id      uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  code         text NOT NULL,                        -- A01 (карта на продукт)
  product_code text,
  narrative_arc text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scan_id, code)
);

CREATE TABLE actant_roles (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  map_id        uuid NOT NULL REFERENCES actant_maps(id) ON DELETE CASCADE,
  role          text NOT NULL CHECK (role IN ('subject','object','sender',
                                              'receiver','helper','opponent')),
  object_level  text CHECK (object_level IN ('functional','emotional','transformational')),
  reconstruction text NOT NULL,
  status        text NOT NULL CHECK (status IN ('F','I','H','U')),
  alternative   text,
  source_codes  jsonb NOT NULL DEFAULT '[]'::jsonb,
  evidence_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
  UNIQUE (map_id, role, object_level)
);

CREATE TABLE journey_steps (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id       uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  ordinal       integer NOT NULL,
  step          text NOT NULL CHECK (step IN ('trigger','interpretation','search',
      'comparison','trust','decision','experience','referral')),
  client_question text NOT NULL,
  public_state  text NOT NULL DEFAULT 'нет данных',
  obstacle      text,
  source_codes  jsonb NOT NULL DEFAULT '[]'::jsonb,
  check_method  text,
  is_observed   boolean NOT NULL DEFAULT false,       -- false = смоделировано
  UNIQUE (scan_id, step), UNIQUE (scan_id, ordinal)
);

CREATE TABLE consistency_cells (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id       uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  actant_role   text NOT NULL,
  dimension     text NOT NULL CHECK (dimension IN ('communication','service',
                                                   'experience','evidence')),
  status        text NOT NULL CHECK (status IN ('confirmed','partial','conflict','no_data')),
  note          text,
  source_codes  jsonb NOT NULL DEFAULT '[]'::jsonb,
  counterexample text,
  UNIQUE (scan_id, actant_role, dimension, note)
);
```

---

## 9. Находки G / M / W

```sql
CREATE TABLE findings_v2 (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id               uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  code                  text NOT NULL,                -- G01 / M01 / W01
  kind                  text NOT NULL
      CHECK (kind IN ('gap','missing_link','weak_evidence')),
  statement             text NOT NULL,
  basis_codes           jsonb NOT NULL DEFAULT '[]'::jsonb,  -- ["S01","E03"]
  consequence           text,
  alternative_explanation text,
  verification          text,
  priority              text NOT NULL DEFAULT 'P2'
      CHECK (priority IN ('P1','P2','P3','blocker')),
  confidence            text NOT NULL DEFAULT 'medium'
      CHECK (confidence IN ('high','medium','low')),
  needs_review          boolean NOT NULL DEFAULT false,
  review_status         text NOT NULL DEFAULT 'machine'
      CHECK (review_status IN ('machine','analyst_edited','approved','rejected')),
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scan_id, code)
);
```

DB-ограничение (триггер `validate_finding`):
`kind='gap'` требует ≥2 записей в `basis_codes`, ссылающихся на разные источники;
`kind='weak_evidence'` требует существующий E-код с `relevance != 'low'`.

---

## 10. Язык, scorecard

```sql
CREATE TABLE language_codes (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id       uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  code          text NOT NULL,                       -- L01
  topic         text NOT NULL,
  professional  text,
  lived         text,
  brand         text,
  match_status  text NOT NULL DEFAULT 'partial'
      CHECK (match_status IN ('match','partial','gap','no_data')),
  is_illustration boolean NOT NULL DEFAULT false,
  source_codes  jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scan_id, code)
);

CREATE TABLE scorecard_entries (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id       uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  ordinal       integer NOT NULL,
  dimension     text NOT NULL CHECK (dimension IN ('clarity','consistency',
                                                   'credibility','resonance')),
  criterion     text NOT NULL,
  score         numeric(3,1),                        -- NULL = N/A, не 0
  na_reason     text,
  confidence    text CHECK (confidence IN ('high','medium','low')),
  rationale     text NOT NULL,
  source_codes  jsonb NOT NULL DEFAULT '[]'::jsonb,
  metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
  CHECK ((score IS NULL) OR (score >= 0 AND score <= 10)),
  CHECK ((score IS NOT NULL) OR (na_reason IS NOT NULL)),
  UNIQUE (scan_id, ordinal)
);
```

Отдельной таблицы агрегата нет — это структурная гарантия отказа от псевдоиндекса.
Покрытие считается на чтении: `count(score) / count(*)` c подписью «покрытие данных».

---

## 11. Рекомендации, KPI, roadmap, gate, гипотезы

```sql
CREATE TABLE recommendations_v2 (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id               uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  code                  text NOT NULL,               -- R01
  change                text NOT NULL,
  rationale             text NOT NULL,
  expected_effect       text,
  expected_effect_status text NOT NULL DEFAULT 'H' CHECK (expected_effect_status = 'H'),
  impact                text CHECK (impact IN ('high','medium','low')),
  evidence_confidence   text CHECK (evidence_confidence IN ('high','medium','low')),
  urgency               text CHECK (urgency IN ('high','medium','low')),
  effort                text CHECK (effort IN ('high','medium','low')),
  priority              text NOT NULL DEFAULT 'P2'
      CHECK (priority IN ('P1','P2','P3','blocker')),
  decision_class        text NOT NULL
      CHECK (decision_class IN ('keep','fix','test','stop')),
  owner                 text,
  kpi_codes             jsonb NOT NULL DEFAULT '[]'::jsonb,
  review_status         text NOT NULL DEFAULT 'machine'
      CHECK (review_status IN ('machine','analyst_edited','approved')),
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scan_id, code)
);

CREATE TABLE recommendation_findings (
  recommendation_id uuid NOT NULL REFERENCES recommendations_v2(id) ON DELETE CASCADE,
  finding_id        uuid NOT NULL REFERENCES findings_v2(id) ON DELETE CASCADE,
  PRIMARY KEY (recommendation_id, finding_id)
);

CREATE TABLE kpis (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id     uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  code        text NOT NULL,                         -- KPI01
  name        text NOT NULL,
  definition  text NOT NULL,
  baseline    text NOT NULL DEFAULT 'unknown',
  period      text,
  data_source text,
  owner       text,
  target      text NOT NULL DEFAULT 'определить после baseline',
  limitations text,
  UNIQUE (scan_id, code)
);

CREATE TABLE roadmap_phases (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id        uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  ordinal        integer NOT NULL,
  title          text NOT NULL,
  dependencies   text,
  actions        text,
  artifact       text,
  owner          text,
  done_criteria  text,
  kpi_codes      jsonb NOT NULL DEFAULT '[]'::jsonb,
  UNIQUE (scan_id, ordinal)
);

CREATE TABLE stage_gate (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id               uuid NOT NULL UNIQUE REFERENCES scans(id) ON DELETE CASCADE,
  decision              text NOT NULL
      CHECK (decision IN ('ready','conditional_ready','not_ready')),
  rationale             text NOT NULL,
  criteria              jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{criterion, result, required}]
  blockers              jsonb NOT NULL DEFAULT '[]'::jsonb,
  required_validations  jsonb NOT NULL DEFAULT '[]'::jsonb,
  approved_by           text,
  decided_at            timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_hypotheses (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id        uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  code           text NOT NULL,                      -- H01
  statement      text NOT NULL,
  how_to_verify  text NOT NULL,
  status         text NOT NULL DEFAULT 'open'
      CHECK (status IN ('open','testing','confirmed','rejected')),
  UNIQUE (scan_id, code)
);

CREATE TABLE open_questions (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id    uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  ordinal    integer NOT NULL,
  question   text NOT NULL,
  target     text NOT NULL DEFAULT 'client' CHECK (target IN ('client','analyst')),
  status     text NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','dropped')),
  UNIQUE (scan_id, ordinal)
);
```

`expected_effect_status` намеренно ограничен `'H'`: обещание причинного эффекта
невозможно записать в БД.

---

## 12. Трассировка, ревью, снимки отчёта

```sql
CREATE TABLE trace_links (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id    uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  from_type  text NOT NULL,
  from_id    uuid NOT NULL,
  to_type    text NOT NULL,
  to_id      uuid NOT NULL,
  relation   text NOT NULL
      CHECK (relation IN ('supports','contradicts','limits','derived_from')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (from_type, from_id, to_type, to_id, relation)
);
CREATE INDEX trace_links_to ON trace_links(to_type, to_id);
CREATE INDEX trace_links_from ON trace_links(from_type, from_id);

CREATE TABLE review_events (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id     uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  entity_type text NOT NULL,
  entity_id   uuid,
  actor       text NOT NULL,
  action      text NOT NULL,      -- edit | approve | reject | reopen | needs_review
  before      jsonb,
  after       jsonb,
  note        text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE report_snapshots (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id        uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  report_version text NOT NULL DEFAULT 'report_v2',
  methodology_version text NOT NULL DEFAULT 'socode_v1.1',
  content_hash   text NOT NULL,
  status         text NOT NULL DEFAULT 'fresh'
      CHECK (status IN ('fresh','needs_rebuild','approved','superseded')),
  document       jsonb NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  approved_at    timestamptz,
  approved_by    text,
  UNIQUE (scan_id, content_hash)
);
CREATE INDEX report_snapshots_latest ON report_snapshots(scan_id, created_at DESC);
```

Инвалидация: любая запись в `review_events` с `action IN ('edit','reopen','needs_review')`
переводит актуальный снимок в `needs_rebuild` (сервисный слой, не триггер — чтобы не
ломать bulk-импорт тестов).

Миграция `0005_report_artifacts` добавляет DB-кэш PDF; неизменный `content_hash` рендерится
один раз, старые артефакты остаются доступными для аудита:

```sql
CREATE TABLE report_artifacts (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id        uuid NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  content_hash   text NOT NULL,
  media_type     text NOT NULL DEFAULT 'application/pdf',
  content        bytea NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scan_id, content_hash, media_type)
);
```

---

## 13. Контракт `report_v2` (JSON)

```json
{
  "report_version": "report_v2",
  "methodology_version": "socode_v1.1",
  "content_hash": "sha256:...",
  "meta": {
    "brand": "...",
    "entry_url": "https://...",
    "audit_mode": "desk",
    "snapshot_date": "2026-11-12",
    "generated_at": "2026-11-12T18:00:00Z",
    "scan_id": "...",
    "status": "internal_draft",
    "disclaimer": "Этот документ не подтверждает реальное поведение клиентов..."
  },
  "sections": [
    {
      "id": "10",
      "title": "Findings Register — G / M / W",
      "anchor": "findings",
      "blocks": [
        {"type": "table", "caption": "...", "columns": [
          {"key": "code", "label": "ID"}, {"key": "kind", "label": "Тип"}],
         "rows": [["G01", "G"]]}
      ],
      "data_gaps": [
        {"section": "11", "missing": "...", "why": "...", "impact": "..."}
      ]
    }
  ],
  "coverage": {"scored": 12, "na": 4, "label": "покрытие данных, не качество бренда"},
  "appendices": {"s_register": [], "e_register": [], "qa_checklist": [], "limits": []}
}
```

Инварианты, проверяемые валидатором перед сохранением снимка:

1. Нет поля агрегированного балла ни на одном уровне.
2. Каждый `paragraph` с `status in (F,I,H,R)` имеет непустые `refs`.
3. Все `refs` существуют в реестрах скана.
4. `audit_mode='desk'` → ни один блок с `status='F'` не описывает наблюдаемый опыт клиента.
5. При `stage_gate.decision='not_ready'` секция 1 не содержит призывов «начинать Stage 02».
6. Список `sections[].id` содержит все обязательные 17 разделов + 4 приложения.

---

## 14. Порядок внедрения

1. Модели в `app/models.py` (по образцу существующих: `Uuid`, `JSONB`, `now()`).
2. `alembic/versions/0002_report_v2.py` — `op.create_table(...)` + триггер `validate_finding`
   + индексы выше.
3. `app/registers/` — сервисы выдачи кодов и CRUD реестров.
4. `app/reports/model_v2.py` / `build_v2.py` / `render.py` (17 разделов + landscape-приложения).
5. `app/analysis/` — стадии `VERIFY…NARRATIVE` (по одной, с тестами на фикстурах).
6. API/UI (`/report`, `/report.json`, `/registers/*`, `/stage-gate`, `/review`).
7. Тесты: структурный эталон Басалык, N/A-дисциплина, трассируемость, стабильность кодов,
   отсутствие агрегата, PDF-landscape.

Старые таблицы (`metrics`, `capitals`, `indices`, `findings`, `recommendations`, `source_profiles`)
не удаляются; используются только при `internal_metrics=true`.
