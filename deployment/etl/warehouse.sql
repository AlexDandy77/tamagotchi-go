-- Lab 5 (contracts/data-warehouse.md): the warehouse's star schema and the ETL's own tables.
-- Applied by the ETL at start; every statement is idempotent.

CREATE SCHEMA IF NOT EXISTS etl;

-- Stage 1: one row per source row, exactly as the owner's etl view returned it.
CREATE TABLE IF NOT EXISTS etl.staging (
  source            TEXT NOT NULL,          -- e.g. tamagotchi
  view              TEXT NOT NULL,          -- e.g. care_actions
  row_key           TEXT NOT NULL,          -- the view's natural key, parts joined with '|'
  data              JSONB NOT NULL,
  source_updated_at TIMESTAMPTZ NOT NULL,   -- the row's watermark column
  loaded_at         TIMESTAMPTZ NOT NULL,   -- the ETL run that last wrote it
  PRIMARY KEY (source, view, row_key)
);
CREATE INDEX IF NOT EXISTS staging_loaded ON etl.staging (source, view, loaded_at);

CREATE TABLE IF NOT EXISTS etl.watermarks (
  source          TEXT NOT NULL,
  view            TEXT NOT NULL,
  last_updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (source, view)
);

CREATE TABLE IF NOT EXISTS etl.runs (
  started_at        TIMESTAMPTZ PRIMARY KEY,
  finished_at       TIMESTAMPTZ NOT NULL,
  rows_staged       INTEGER NOT NULL,
  facts_recomputed  INTEGER NOT NULL,
  sources_skipped   TEXT[] NOT NULL
);

-- Dimensions.
CREATE TABLE IF NOT EXISTS dim_date (
  date_key INTEGER PRIMARY KEY,  -- YYYYMMDD
  date     DATE NOT NULL,
  year     INTEGER NOT NULL,
  month    INTEGER NOT NULL,
  day      INTEGER NOT NULL,
  weekday  INTEGER NOT NULL      -- ISO: 1 = Monday
);
CREATE TABLE IF NOT EXISTS dim_user (
  user_id           TEXT PRIMARY KEY,
  username          TEXT,
  first_enrolled_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS dim_package (
  package_id UUID PRIMARY KEY,
  name       TEXT,
  status     TEXT
);
CREATE TABLE IF NOT EXISTS dim_guild (
  guild_id UUID PRIMARY KEY,
  name     TEXT
);
CREATE TABLE IF NOT EXISTS dim_monster (
  monster_id UUID PRIMARY KEY,
  name       TEXT,
  type       TEXT
);

-- Facts. `complete` is false while an input they join is still missing.
CREATE TABLE IF NOT EXISTS fact_battle (
  battle_id        UUID PRIMARY KEY,
  date_key         INTEGER NOT NULL,
  package_id       UUID,
  challenger_id    TEXT,
  opponent_id      TEXT,
  winner_id        TEXT,
  loser_id         TEXT,
  status           TEXT NOT NULL,
  turns            INTEGER,
  duration_seconds INTEGER,
  captured_pet_id  UUID,
  complete         BOOLEAN NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_care_action (
  operation_id UUID PRIMARY KEY,
  date_key     INTEGER NOT NULL,
  user_id      TEXT NOT NULL,
  package_id   UUID,
  pet_id       UUID,
  pet_type     TEXT,
  action_id    TEXT,
  xp_gained    INTEGER,
  complete     BOOLEAN NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_raid_participation (
  raid_id      UUID NOT NULL,
  user_id      TEXT NOT NULL,
  date_key     INTEGER NOT NULL,
  guild_id     UUID,
  monster_id   UUID,
  damage       INTEGER,
  reward_coins INTEGER,
  won          BOOLEAN,
  complete     BOOLEAN NOT NULL,
  PRIMARY KEY (raid_id, user_id)
);
CREATE TABLE IF NOT EXISTS fact_guild_membership (
  guild_id        UUID NOT NULL,
  user_id         TEXT NOT NULL,
  role            TEXT,
  joined_date_key INTEGER NOT NULL,
  left_date_key   INTEGER,
  PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS fact_currency_movement (
  entry_id   UUID PRIMARY KEY,
  date_key   INTEGER NOT NULL,
  user_id    TEXT NOT NULL,
  package_id UUID,
  currency   TEXT NOT NULL,
  amount     BIGINT NOT NULL,
  reason     TEXT
);
CREATE TABLE IF NOT EXISTS fact_notification (
  notification_id    UUID PRIMARY KEY,
  date_key           INTEGER NOT NULL,
  user_id            TEXT NOT NULL,
  type               TEXT NOT NULL,
  read_after_seconds INTEGER
);
