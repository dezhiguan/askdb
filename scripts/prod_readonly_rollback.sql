-- 撤销 askdb 在云上 ragforge 库（8.163.30.216）所做的全部改动。
-- 执行：
--   ssh root@8.163.30.216 'docker exec -i ragforge-postgres \
--     psql -U ragforge -d ragforge' < scripts/prod_readonly_rollback.sql

\set ON_ERROR_STOP on

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

SELECT 'askdb 相关对象已全部移除' AS 结果;
