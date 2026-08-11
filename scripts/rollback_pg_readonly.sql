-- 撤销 askdb 在本机 ragforge 库上做的全部改动。
-- 用法：psql -d ragforge -f scripts/rollback_pg_readonly.sql

DROP POLICY IF EXISTS askdb_org_isolation ON organizations;
DROP POLICY IF EXISTS askdb_org_isolation ON knowledge_bases;
DROP POLICY IF EXISTS askdb_org_isolation ON documents;
DROP POLICY IF EXISTS askdb_org_isolation ON retrieval_logs;
DROP POLICY IF EXISTS askdb_org_isolation ON model_usage_daily;

ALTER TABLE organizations     DISABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_bases   DISABLE ROW LEVEL SECURITY;
ALTER TABLE documents         DISABLE ROW LEVEL SECURITY;
ALTER TABLE retrieval_logs    DISABLE ROW LEVEL SECURITY;
ALTER TABLE model_usage_daily DISABLE ROW LEVEL SECURITY;

DROP OWNED BY askdb_ro;
DROP ROLE IF EXISTS askdb_ro;
