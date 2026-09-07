-- askdb 数据源注册表的元数据库 —— 云上 ragforge PG（8.163.30.216，容器 ragforge-postgres）
-- 拟制日期：2026-09-07
--
-- 为什么需要它：2026-09-06 起数据源从 var/sources/*.yaml 改存 PostgreSQL
-- （两副本共享 hostPath 且 yaml 写入无锁，探活整文件重写会把白名单带脏）。
-- 连接串只从环境变量 ASKDB_SOURCES_DSN 读 —— 没配这个变量，
-- 所有 /api/sources 接口一律 503（sources_store_unavailable），服务本身照起。
-- deploy/k8s/askdb.yaml 里那条「⚠️ 上线前必须先建这个 Secret」说的就是它。
--
-- **单独一个库，不与 ragforge 业务库同库。** 理由是权限方向相反：
-- askdb 对业务库只该有 SELECT，而它对自己的元数据必须能写。
-- 混在一个库里，就得给同一套部署既发只读账号又发可写账号，
-- 迟早有人把可写那把配到 datasource 上去。
--
-- 表结构不在这里建：askdb_sources 由应用的 sources.ensure_schema() 幂等建表
-- （CREATE TABLE IF NOT EXISTS），这里只准备库、角色与建表权限。
--
-- 本脚本不含任何凭据：密码经 psql 变量 :pwd 在执行时传入。
--
-- 执行**必须用超级用户**：末尾要在新库里 GRANT schema 权限，
-- PG 15 起 public schema 不再默认对所有人开放 CREATE，
-- 不授的话应用起来后 ensure_schema() 建表会 permission denied。
--
--   PWD='<新生成的强口令>'
--   ssh root@8.163.30.216 "docker exec -i ragforge-postgres \
--     psql -U postgres -d postgres -v pwd=\"'$PWD'\"" < scripts/askdb_sources_store_setup.sql
--
-- 回滚：scripts/askdb_multi_source_rollback.sql

\set ON_ERROR_STOP on

-- 角色。幂等：已存在就只重设口令，不 DROP ——
-- DROP 会连着把 askdb_sources 表一起带走（属主是它），而那张表里是已注册的
-- 数据源与表白名单，重跑一次建库脚本不该把它们清掉。
--
-- 用 \gexec 拼语句而不是 DO 块：DO 的函数体是美元引用字符串，psql 的 :pwd
-- **不会**在里面展开，写成 DO 的话建出来的角色口令会是字面量 ":pwd"。
-- 调用方传的是 -v pwd="'$PWD'"，:pwd 展开后本身就是一个 SQL 字符串字面量，
-- 所以这里对它取值再 quote_literal 一次，转义交给库来做。
SELECT 'ALTER ROLE askdb_meta PASSWORD ' || quote_literal(:pwd)
 WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'askdb_meta')\gexec

SELECT 'CREATE ROLE askdb_meta LOGIN PASSWORD ' || quote_literal(:pwd)
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'askdb_meta')\gexec

-- CREATE DATABASE 不能在事务块里跑，用 \gexec 绕开，并保持幂等
SELECT 'CREATE DATABASE askdb_meta OWNER askdb_meta'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'askdb_meta')\gexec

GRANT CONNECT ON DATABASE askdb_meta TO askdb_meta;

-- 连接数：两副本 × 池 max_size 4 = 8，留一点余量给运维手工连。
-- 不设的话默认无限，一次连接泄漏就能把整台 PG 的连接位吃光 ——
-- 而这台 PG 上还跑着 ragforge 与 careermate 的业务库。
ALTER ROLE askdb_meta CONNECTION LIMIT 12;

-- 元数据库要能写，但**不该能读业务库**：这里不给它 ragforge / careermate 的
-- 任何权限，方向上就堵死"元数据账号被拿去当数据源连接串"这条路。
ALTER ROLE askdb_meta SET statement_timeout = '10s';
ALTER ROLE askdb_meta SET idle_in_transaction_session_timeout = '30s';

-- ---- 建表权限 ----
-- 切进新库再授。PG 15 起 public schema 的 CREATE 不再默认给 PUBLIC，
-- 而 askdb_sources 是由应用启动后 ensure_schema() 现建的 ——
-- 少这一句，症状是数据源页 503 且日志里 permission denied for schema public，
-- 看着像"元数据库没配好"，实际库和角色都在，只差一个授权。
\connect askdb_meta

GRANT ALL ON SCHEMA public TO askdb_meta;

SELECT 'askdb_meta 角色与库已就绪（当前库 ' || current_database()
       || '），askdb_sources 表由应用 ensure_schema() 自建' AS 结果;
