-- Disaster Information Center · skema v1
-- Dijalankan otomatis saat aplikasi start (idempotent). PostgreSQL 12+.
-- Tidak memerlukan PostGIS: geometri disimpan sebagai GeoJSON (jsonb) dan
-- dianalisis oleh modul dcc.geo.

CREATE TABLE IF NOT EXISTS schema_migrations (
  version     integer PRIMARY KEY,
  applied_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
  id             bigserial PRIMARY KEY,
  username       text NOT NULL UNIQUE,
  display_name   text NOT NULL DEFAULT '',
  role           text NOT NULL CHECK (role IN ('admin','user')),
  password_hash  text NOT NULL,
  active         boolean NOT NULL DEFAULT true,
  must_change_password boolean NOT NULL DEFAULT false,
  phone          text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  last_login_at  timestamptz
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash   text PRIMARY KEY,
  user_id      bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  ip           text,
  user_agent   text
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);

CREATE TABLE IF NOT EXISTS audit_events (
  id           bigserial PRIMARY KEY,
  at           timestamptz NOT NULL DEFAULT now(),
  actor_id     bigint,
  actor_name   text,
  action       text NOT NULL,
  entity_type  text,
  entity_id    text,
  detail       jsonb,
  ip           text
);
CREATE INDEX IF NOT EXISTS audit_at_idx ON audit_events(at DESC);

CREATE TABLE IF NOT EXISTS app_settings (
  key         text PRIMARY KEY,
  value       jsonb NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  updated_by  text
);

-- ---------------------------------------------------------------- aset
CREATE TABLE IF NOT EXISTS asset_snapshots (
  id            bigserial PRIMARY KEY,
  source        text NOT NULL,            -- kml | arcgis
  source_name   text,
  started_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz,
  state         text NOT NULL DEFAULT 'running',  -- running | published | failed
  counts        jsonb,
  error         text,
  created_by    text
);

CREATE TABLE IF NOT EXISTS assets (
  id               bigserial PRIMARY KEY,
  source_key       text NOT NULL UNIQUE,
  kind             text NOT NULL,   -- gi gh recloser lbs trafo jtm ulp pltd kantor
  name             text NOT NULL DEFAULT '',
  up3              text,
  ulp              text,
  feeder           text,
  gardu_id         text,
  code             text,
  capacity_kva     double precision,
  status           text,
  lon              double precision,
  lat              double precision,
  bbox             jsonb,
  geometry         jsonb NOT NULL,
  props            jsonb NOT NULL DEFAULT '{}'::jsonb,
  first_snapshot_id bigint,
  last_snapshot_id  bigint,
  active           boolean NOT NULL DEFAULT true,
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assets_kind_idx ON assets(kind) WHERE active;
CREATE INDEX IF NOT EXISTS assets_feeder_idx ON assets(feeder);
CREATE INDEX IF NOT EXISTS assets_gardu_idx ON assets(gardu_id);
CREATE INDEX IF NOT EXISTS assets_name_idx ON assets(lower(name));

CREATE TABLE IF NOT EXISTS customer_count_sets (
  id              bigserial PRIMARY KEY,
  filename        text,
  key_field       text,
  count_field     text,
  uploaded_by     text,
  uploaded_at     timestamptz NOT NULL DEFAULT now(),
  effective_date  date,
  rows_total      integer,
  rows_matched    integer,
  rows_unmatched  integer,
  unmatched_sample jsonb,
  active          boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS customer_counts (
  set_id    bigint NOT NULL REFERENCES customer_count_sets(id) ON DELETE CASCADE,
  asset_id  bigint NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  count     integer CHECK (count IS NULL OR count >= 0),
  PRIMARY KEY (set_id, asset_id)
);

-- --------------------------------------------------------------- cuaca
CREATE TABLE IF NOT EXISTS weather_alerts (
  id            text PRIMARY KEY,
  sent          timestamptz,
  effective     timestamptz,
  expires       timestamptz,
  status        text,
  msg_type      text,
  refs          jsonb NOT NULL DEFAULT '[]'::jsonb,
  title         text,
  description   text,
  instruction   text,
  severity      text,
  certainty     text,
  urgency       text,
  areas         jsonb NOT NULL DEFAULT '[]'::jsonb,
  areas_text    jsonb NOT NULL DEFAULT '[]'::jsonb,
  source_link   text,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  fetched_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS weather_alerts_exp_idx ON weather_alerts(expires DESC);

CREATE TABLE IF NOT EXISTS alert_exposures (
  alert_id     text NOT NULL REFERENCES weather_alerts(id) ON DELETE CASCADE,
  asset_id     bigint NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  computed_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (alert_id, asset_id)
);

CREATE TABLE IF NOT EXISTS alert_reviews (
  id          bigserial PRIMARY KEY,
  alert_id    text NOT NULL,
  user_id     bigint REFERENCES users(id),
  user_name   text,
  reviewed_at timestamptz NOT NULL DEFAULT now(),
  note        text
);

CREATE TABLE IF NOT EXISTS source_health (
  source      text PRIMARY KEY,
  state       text NOT NULL,      -- ok | partial | error | disabled | unknown
  checked_at  timestamptz,
  ok_at       timestamptz,
  error       text,
  detail      jsonb
);

-- ------------------------------------------------------------ kejadian
CREATE TABLE IF NOT EXISTS incident_categories (
  code    text PRIMARY KEY,
  label   text NOT NULL,
  color   text NOT NULL DEFAULT '#0284c7',
  sort    integer NOT NULL DEFAULT 100,
  active  boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS incidents (
  id           bigserial PRIMARY KEY,
  title        text NOT NULL,
  category     text NOT NULL REFERENCES incident_categories(code),
  lon          double precision NOT NULL,
  lat          double precision NOT NULL,
  occurred_at  timestamptz NOT NULL,
  severity     text NOT NULL DEFAULT 'sedang' CHECK (severity IN ('rendah','sedang','tinggi','kritis')),
  status       text NOT NULL DEFAULT 'dilaporkan' CHECK (status IN ('dilaporkan','diverifikasi','penanganan','selesai')),
  description  text NOT NULL DEFAULT '',
  pic          text NOT NULL DEFAULT '',
  notes        text NOT NULL DEFAULT '',
  visibility   text NOT NULL DEFAULT 'internal' CHECK (visibility IN ('internal','guest')),
  archived     boolean NOT NULL DEFAULT false,
  revision     integer NOT NULL DEFAULT 1,
  created_by   text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_by   text,
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS incidents_occ_idx ON incidents(occurred_at DESC);

CREATE TABLE IF NOT EXISTS incident_revisions (
  incident_id  bigint NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  revision     integer NOT NULL,
  data         jsonb NOT NULL,
  edited_by    text,
  edited_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (incident_id, revision)
);

CREATE TABLE IF NOT EXISTS incident_areas (
  id           bigserial PRIMARY KEY,
  incident_id  bigint REFERENCES incidents(id) ON DELETE SET NULL,
  name         text NOT NULL,
  category     text,
  color        text NOT NULL DEFAULT '#0ea5e9',
  geometry     jsonb NOT NULL,
  bbox         jsonb NOT NULL,
  area_km2     double precision,
  visibility   text NOT NULL DEFAULT 'internal' CHECK (visibility IN ('internal','guest')),
  archived     boolean NOT NULL DEFAULT false,
  revision     integer NOT NULL DEFAULT 1,
  created_by   text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_by   text,
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS incident_area_revisions (
  area_id    bigint NOT NULL REFERENCES incident_areas(id) ON DELETE CASCADE,
  revision   integer NOT NULL,
  name       text,
  geometry   jsonb NOT NULL,
  edited_by  text,
  edited_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (area_id, revision)
);

CREATE TABLE IF NOT EXISTS incident_photos (
  id            bigserial PRIMARY KEY,
  incident_id   bigint NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  original_name text,
  object_key    text NOT NULL,
  thumb_key     text NOT NULL,
  content_type  text NOT NULL,
  bytes         integer NOT NULL,
  sha256        text NOT NULL,
  width         integer,
  height        integer,
  caption       text NOT NULL DEFAULT '',
  taken_at      timestamptz,
  uploaded_by   text,
  uploaded_at   timestamptz NOT NULL DEFAULT now(),
  visibility    text NOT NULL DEFAULT 'internal' CHECK (visibility IN ('internal','guest')),
  archived      boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS photos_incident_idx ON incident_photos(incident_id);

CREATE TABLE IF NOT EXISTS impact_analyses (
  id                  bigserial PRIMARY KEY,
  incident_id         bigint REFERENCES incidents(id) ON DELETE SET NULL,
  area_refs           jsonb NOT NULL,     -- [{id, revision}]
  asset_snapshot_id   bigint,
  customer_set_id     bigint,
  rule_version        text NOT NULL,
  computed_at         timestamptz NOT NULL DEFAULT now(),
  computed_by         text,
  result              jsonb NOT NULL,
  published_guest     boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS impact_analysis_assets (
  analysis_id           bigint NOT NULL REFERENCES impact_analyses(id) ON DELETE CASCADE,
  asset_id              bigint NOT NULL,
  known_customer_count  integer,
  PRIMARY KEY (analysis_id, asset_id)
);

-- ------------------------------------------------------- notifikasi WA
CREATE TABLE IF NOT EXISTS notifications (
  id              bigserial PRIMARY KEY,
  channel         text NOT NULL DEFAULT 'wa',
  target          text NOT NULL,
  message         text NOT NULL,
  dedupe_key      text NOT NULL UNIQUE,
  status          text NOT NULL DEFAULT 'pending',  -- pending | sent | failed | skipped
  attempts        integer NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  created_at      timestamptz NOT NULL DEFAULT now(),
  sent_at         timestamptz,
  error           text
);
CREATE INDEX IF NOT EXISTS notifications_pending_idx ON notifications(next_attempt_at) WHERE status = 'pending';

INSERT INTO incident_categories(code,label,color,sort) VALUES
  ('banjir','Banjir','#2563eb',10),
  ('longsor','Tanah longsor','#a16207',20),
  ('pohon_tumbang','Pohon tumbang','#15803d',30),
  ('petir','Sambaran petir','#7c3aed',40),
  ('angin_kencang','Angin kencang','#0891b2',50),
  ('gempa','Gempa bumi','#b91c1c',60),
  ('kebakaran','Kebakaran','#ea580c',70),
  ('gangguan_lain','Lainnya','#475569',99)
ON CONFLICT (code) DO NOTHING;

INSERT INTO schema_migrations(version) VALUES (1) ON CONFLICT DO NOTHING;
