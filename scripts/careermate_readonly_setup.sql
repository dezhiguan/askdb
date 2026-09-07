-- askdb 只读接入 —— careermate 生产库（8.163.30.216，容器 ragforge-postgres，库 careermate_db）
-- 拟制日期：2026-09-07
--
-- careermate 与 ragforge 是**同一台 PostgreSQL 上的两个库**，不是两台机器。
-- 在此之前 careermate_db 没有任何只读角色（scripts/prod_readonly_setup.sql 只
-- 建了 askdb_ro，且 CONNECT 只授到 ragforge 库），所以这是从零建一套。
--
-- ⚠️ 该实例无只读副本，这是**主库直连**，与 askdb_ro 同一个例外口径。
--
-- ⚠️ **这个库里是注册用户的个人数据**：users.phone / users.email /
--    users.password_hash、resumes 与 resume_versions 的简历全文、
--    agent_messages 的完整对话、user_long_term_memory 的用户画像。
--    开放范围由 askdb 侧的表白名单决定（PUT /api/sources/{sid}/tables），
--    库这一层只负责"只读"。改白名单前先想清楚谁能登录 askdb ——
--    config/public.yaml 的 auth.required 必须是 true，那是行级边界撤掉之后
--    唯一挡在公网与这些数据之间的东西。
--
-- 执行**必须用超级用户或 careermate 属主**：GRANT ... ON ALL TABLES 要求
-- 对每张表有属主权限，用 -U ragforge 会静默只授到它自己拥有的那些表上。
--
--   PWD='<新生成的强口令>'
--   ssh root@8.163.30.216 "docker exec -i ragforge-postgres \
--     psql -U ragforge -d careermate_db -v pwd=\"'$PWD'\"" < scripts/careermate_readonly_setup.sql
--
-- 回滚：scripts/askdb_multi_source_rollback.sql

\set ON_ERROR_STOP on

-- 幂等：重复执行先清干净。
-- DROP OWNED BY 在角色不存在时会报错，必须先判断存在性。
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careermate_ro') THEN
    EXECUTE 'DROP OWNED BY careermate_ro';
    EXECUTE 'DROP ROLE careermate_ro';
  END IF;
END $$;

SELECT 'CREATE ROLE careermate_ro LOGIN PASSWORD ' || quote_literal(:pwd)\gexec

GRANT CONNECT ON DATABASE careermate_db TO careermate_ro;
GRANT USAGE   ON SCHEMA public          TO careermate_ro;

-- 全表 SELECT，按 2026-09-07 @guandezhi 决定（askdb 侧白名单再收）。
-- **有意不设 ALTER DEFAULT PRIVILEGES**：将来新建的表不自动开放。
-- 自动开放意味着 careermate 加一张表就等于在 askdb 上多暴露一张，
-- 而那一步没有任何人会意识到自己在改暴露面。新表要开，重跑一次这个脚本，
-- 并在 askdb 侧重新扫描 + 显式加进白名单。
GRANT SELECT ON ALL TABLES IN SCHEMA public TO careermate_ro;

-- ---- 引擎层硬护栏（与 askdb_ro 同档）----
ALTER ROLE careermate_ro SET default_transaction_read_only = on;
ALTER ROLE careermate_ro SET statement_timeout = '5s';
ALTER ROLE careermate_ro SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE careermate_ro SET lock_timeout = '2s';
-- askdb_ro 当年设的是 2。那时只有一个数据源、一个内置连接；现在两副本各自
-- 持一个连接池（max_size 4），2 会在正常使用下就把人挡在外面。
ALTER ROLE careermate_ro CONNECTION LIMIT 8;

-- ---- 行级安全：不设 ----
-- ragforge 那边的 RLS 绑的是 org_id，而 careermate 是按 user_id 切分的单租户
-- 产品，askdb 的运行时数据源又一律不做租户隔离（sources.derive_config 写死）。
-- 在这里建一套没人会去 SET 的策略，只会造成"库侧还有一层"的错觉 ——
-- 实际效果是所有查询恒零行，而且不报错。要收窄，收在表白名单上。

SELECT 'careermate_ro 已创建，SELECT 授到 public 下 '
       || count(*) || ' 张表；未设 RLS（见脚本末尾说明）' AS 结果
  FROM information_schema.tables
 WHERE table_schema = 'public' AND table_type = 'BASE TABLE';
