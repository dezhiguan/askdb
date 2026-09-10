-- askdb 只读接入 —— ragforge 生产库全库只读角色
-- （8.163.30.216，容器 ragforge-postgres，库 ragforge）
-- 拟制日期：2026-09-07
--
-- 为什么不直接复用 askdb_ro：**它在运行时数据源上会恒零行，且不报错。**
--
--   askdb_ro 那 5 张表上绑了 RLS 策略 askdb_org_isolation，条件是
--   current_setting('app.org_id', true)，由内置数据源那条链路负责 SET。
--   而 askdb 的运行时数据源一律不做租户隔离（sources.derive_config 写死
--   tenant.enabled=False），根本不会去 SET app.org_id ——
--   于是策略取到 NULL，一行都匹配不上。
--
--   症状是最坏的那一种：连得上、表扫得出、查询不报错、结果永远为空。
--   照搬 askdb_ro 去注册运行时源，会得到一个看着完全正常的空数据源。
--
-- 所以这里另建一个角色，并给它显式的放行策略。askdb_ro **保持原样不动**：
-- 它仍然是"按 org_id 收窄"的那把钥匙，两把钥匙的语义不同，不要合并。
--
-- ⚠️ 该实例无只读副本，这是**主库直连**，与 askdb_ro 同一个例外口径。
--
-- ⚠️ 这把钥匙看得到**全部组织**的数据，包括各组织上传的文档正文。
--    开放范围由 askdb 侧表白名单决定；config/public.yaml 的
--    auth.query_requires_login 必须是 true（2026-09-10 起的口径；
--    required 已定为 false）—— 行级边界在这条链路上已经没有了。
--
-- 执行（超级用户或 ragforge 属主）：
--   PWD='<新生成的强口令>'
--   ssh root@8.163.30.216 "docker exec -i ragforge-postgres \
--     psql -U ragforge -d ragforge -v pwd=\"'$PWD'\"" < scripts/ragforge_readonly_all_setup.sql
--
-- 回滚：scripts/askdb_multi_source_rollback.sql

\set ON_ERROR_STOP on

-- 幂等：重复执行先清干净。DROP OWNED BY 会连着把下面那几条策略一并删掉，
-- 所以策略在角色重建之后再建，顺序不能反。
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ragforge_ro_all') THEN
    EXECUTE 'DROP OWNED BY ragforge_ro_all';
    EXECUTE 'DROP ROLE ragforge_ro_all';
  END IF;
END $$;

SELECT 'CREATE ROLE ragforge_ro_all LOGIN PASSWORD ' || quote_literal(:pwd)\gexec

GRANT CONNECT ON DATABASE ragforge TO ragforge_ro_all;
GRANT USAGE   ON SCHEMA public     TO ragforge_ro_all;

-- 全表 SELECT，按 2026-09-07 @guandezhi 决定。
-- 与 askdb_ro 的逐表授权相反 —— 那把钥匙是"5 张表 + 按组织收窄"，
-- 这把是"全库 + 不收窄"，差别写在角色名里（_ro vs _ro_all）。
--
-- 有意不设 ALTER DEFAULT PRIVILEGES，理由同 careermate_readonly_setup.sql：
-- ragforge 加一张表不该等于 askdb 上自动多暴露一张。
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ragforge_ro_all;

-- ---- 引擎层硬护栏 ----
ALTER ROLE ragforge_ro_all SET default_transaction_read_only = on;
ALTER ROLE ragforge_ro_all SET statement_timeout = '5s';
ALTER ROLE ragforge_ro_all SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE ragforge_ro_all SET lock_timeout = '2s';
ALTER ROLE ragforge_ro_all CONNECTION LIMIT 8;

-- ---- 放行策略 ----
-- 那 5 张表 ENABLE ROW LEVEL SECURITY 之后，**没有匹配策略的角色一行都读不到**
-- （RLS 的默认是拒绝，不是放行）。所以这里必须显式给一条 USING (true)，
-- 否则上面那句 GRANT SELECT 等于没授。
--
-- 只对这 5 张表建：库里其余的表压根没开 RLS，不需要策略，
-- 给它们建反而会让人以为那些表也在策略管控之下。
CREATE POLICY askdb_all_orgs ON organizations     FOR SELECT TO ragforge_ro_all USING (true);
CREATE POLICY askdb_all_orgs ON knowledge_bases   FOR SELECT TO ragforge_ro_all USING (true);
CREATE POLICY askdb_all_orgs ON documents         FOR SELECT TO ragforge_ro_all USING (true);
CREATE POLICY askdb_all_orgs ON retrieval_logs    FOR SELECT TO ragforge_ro_all USING (true);
CREATE POLICY askdb_all_orgs ON model_usage_daily FOR SELECT TO ragforge_ro_all USING (true);

-- 自证：askdb_org_isolation 必须还在（askdb_ro 那条链路不受本脚本影响），
-- 且新策略正好 5 条。数目不对就是上面某张表改过名或没开 RLS。
SELECT 'ragforge_ro_all 已创建；放行策略 '
       || count(*) FILTER (WHERE policyname = 'askdb_all_orgs') || ' 条，'
       || 'askdb_ro 原隔离策略 '
       || count(*) FILTER (WHERE policyname = 'askdb_org_isolation') || ' 条' AS 结果
  FROM pg_policies WHERE schemaname = 'public';
