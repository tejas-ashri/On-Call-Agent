-- roles.sql — cluster-global roles (doc 02 §3.1). Run once as a superuser. Idempotent.
--
-- The single most important line in the whole data model is NOBYPASSRLS on the role the
-- application actually uses: a superuser or BYPASSRLS role would render every policy
-- cosmetic. We separate three roles exactly as doc 02 §3.1 prescribes.

-- The application role: can read/write tenant data but CANNOT bypass RLS.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sentinel_app') THEN
        CREATE ROLE sentinel_app NOLOGIN NOBYPASSRLS;
    END IF;
END $$;

-- The actual LOGIN role the connection pool uses; it INHERITs sentinel_app's grants.
-- NOSUPERUSER + NOBYPASSRLS is what makes RLS bind every statement it issues.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sentinel_runtime') THEN
        CREATE ROLE sentinel_runtime LOGIN PASSWORD 'sentinel'
            NOSUPERUSER NOBYPASSRLS INHERIT;
    END IF;
END $$;

GRANT sentinel_app TO sentinel_runtime;

-- The app must be able to USE the schema (table-level grants live in policies.sql).
GRANT USAGE ON SCHEMA public TO sentinel_app;
