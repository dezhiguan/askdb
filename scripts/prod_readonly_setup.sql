-- askdb 只读接入 —— 云上 ragforge 库（8.163.30.216，Docker 容器 ragforge-postgres）
-- 拟制日期：2026-08-12
--
-- ⚠️ 该实例无只读副本：pg_is_in_recovery() = false，复制槽数 = 0。
--    因此这是**主库直连**。技术设计说明书 §8 将此列为禁止项，
--    本次为使用方明确要求后的例外，参数已按主库场景比本机再收紧一档。
--
-- 本脚本不含任何凭据：密码经 psql 变量 :pwd 在执行时传入。
--
-- 执行（密码取自本机 .env 的 ASKDB_PROD_PG_PASSWORD）：
--   PWD=$(grep '^ASKDB_PROD_PG_PASSWORD=' .env | cut -d= -f2-)
--   ssh root@8.163.30.216 "docker exec -i ragforge-postgres \
--     psql -U ragforge -d ragforge -v pwd=\"'$PWD'\"" < scripts/prod_readonly_setup.sql
--
-- 回滚：scripts/prod_readonly_rollback.sql（同样的执行方式，无需 -v）

\set ON_ERROR_STOP on

-- 幂等：重复执行先清干净。
-- DROP OWNED BY 在角色不存在时会报错，必须先判断存在性。
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'askdb_ro') THEN
    EXECUTE 'DROP OWNED BY askdb_ro';
    EXECUTE 'DROP ROLE askdb_ro';
  END IF;
END $$;

CREATE ROLE askdb_ro LOGIN PASSWORD :pwd;

GRANT CONNECT ON DATABASE ragforge TO askdb_ro;
GRANT USAGE   ON SCHEMA public     TO askdb_ro;

-- 逐表授权，禁止 ALL TABLES。
-- document_chunks 有 1,299,154 行且存正文，一律不开放。
GRANT SELECT ON organizations, knowledge_bases, documents,
                retrieval_logs, model_usage_daily TO askdb_ro;

-- ---- 引擎层硬护栏（比本机开发库的 8s / 3 连接更严）----
ALTER ROLE askdb_ro SET default_transaction_read_only = on;
ALTER ROLE askdb_ro SET statement_timeout = '5s';
ALTER ROLE askdb_ro SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE askdb_ro SET lock_timeout = '2s';
ALTER ROLE askdb_ro CONNECTION LIMIT 2;

-- ---- 行级安全：应用层 AST 改写之外的第二道 ----
-- 表属主是 ragforge，默认不受策略约束 —— 现有应用完全不受影响。
ALTER TABLE organizations     ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_bases   ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents         ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrieval_logs    ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_usage_daily ENABLE ROW LEVEL SECURITY;

CREATE POLICY askdb_org_isolation ON organizations FOR SELECT TO askdb_ro
  USING (id = current_setting('app.org_id', true)::bigint);

CREATE POLICY askdb_org_isolation ON knowledge_bases FOR SELECT TO askdb_ro
  USING (org_id = current_setting('app.org_id', true)::bigint);

-- documents 没有 org_id，经 kb_id 间接归属到知识库所属组织
CREATE POLICY askdb_org_isolation ON documents FOR SELECT TO askdb_ro
  USING (kb_id IN (SELECT id FROM knowledge_bases
                   WHERE org_id = current_setting('app.org_id', true)::bigint));

CREATE POLICY askdb_org_isolation ON retrieval_logs FOR SELECT TO askdb_ro
  USING (org_id = current_setting('app.org_id', true)::bigint);

CREATE POLICY askdb_org_isolation ON model_usage_daily FOR SELECT TO askdb_ro
  USING (org_id = current_setting('app.org_id', true)::bigint);

SELECT 'askdb_ro 已创建；RLS 已绑定 5 张表' AS 结果;
