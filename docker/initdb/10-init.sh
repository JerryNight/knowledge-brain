#!/usr/bin/env bash
#
# First-boot provisioning for the knowledge-brain database.
#
# The postgres image runs everything in /docker-entrypoint-initdb.d exactly once,
# as POSTGRES_USER against POSTGRES_DB. That is the right home for anything that
# needs superuser or is per-database — `CREATE EXTENSION` is both.
#
# Why the application role is created here and not in a migration:
# migration 0001 GRANTs table privileges to `kb_app`, so the role has to exist
# first. Keeping it in the image also means every container built from this
# image — docker compose or testcontainers — is usable straight away.
set -euo pipefail

KB_APP_PASSWORD="${KB_APP_PASSWORD:-kb_app_pw}"

echo "[kb-init] creating extensions and the 'chinese' text search configuration"

# Quoted heredoc: $$ is passed through literally, and the only variables we need
# (POSTGRES_USER / POSTGRES_DB) are expanded by bash on the psql command line.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
-- vector: the embedding column. zhparser: Chinese full-text search (spec §5).
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS zhparser;

-- zhparser ships a parser, not a configuration — one has to be declared.
-- `chinese` is the name the queries and the generated chunks.tsv column expect.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'chinese') THEN
        CREATE TEXT SEARCH CONFIGURATION chinese (PARSER = zhparser);
        -- zhparser's standard mapping: keep nouns, verbs, adjectives, idioms,
        -- interjections and temporary words; drop particles and punctuation.
        ALTER TEXT SEARCH CONFIGURATION chinese ADD MAPPING FOR n,v,a,i,e,l WITH simple;
    END IF;
END
$$;
SQL

echo "[kb-init] creating application role 'kb_app' (NOSUPERUSER, NOBYPASSRLS)"

# The application role is deliberately neither a superuser nor the owner of any
# table. Table owners bypass RLS unless the table says FORCE, and superusers
# always bypass it — an app connecting as POSTGRES_USER would silently defeat
# every tenant policy in migration 0001.
#
# `\gexec` rather than a DO block: psql does not interpolate `:variables` inside
# dollar-quoted strings, so the password would never make it in.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_password="$KB_APP_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE kb_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L', :'app_password')
  FROM (SELECT 1) AS _
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_app')
\gexec

SELECT format('ALTER ROLE kb_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L', :'app_password')
  FROM (SELECT 1) AS _
 WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_app')
\gexec
SQL

echo "[kb-init] verifying Chinese segmentation (M2.2 acceptance)"

# If the configuration is wrong, fail here rather than three milestones later
# when retrieval mysteriously returns nothing useful.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -tA <<'SQL'
SELECT 'extensions: ' || string_agg(extname, ', ' ORDER BY extname) FROM pg_extension;
SELECT 'chinese config present: ' || EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'chinese');
SELECT 'segmented: ' || to_tsvector('chinese', '防止任务重复执行'::text)::text;
SQL

echo "[kb-init] done"
