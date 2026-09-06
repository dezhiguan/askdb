-- 回滚 2026-09-07 的多数据源接入：撤掉 careermate_ro / ragforge_ro_all / askdb_meta
-- 拟制日期：2026-09-07
--
-- 三个对象分处三个库，**必须分三次执行**（psql 一次只连一个库）：
--
--   ssh root@8.163.30.216 "docker exec -i ragforge-postgres \
--     psql -U postgres -d careermate_db" < scripts/askdb_multi_source_rollback.sql
--   ssh root@8.163.30.216 "docker exec -i ragforge-postgres \
--     psql -U ragforge -d ragforge"      < scripts/askdb_multi_source_rollback.sql
--   ssh root@8.163.30.216 "docker exec -i ragforge-postgres \
--     psql -U postgres -d postgres"      < scripts/askdb_multi_source_rollback.sql
--
-- 每次只有与当前库相关的那一段会做事，其余段落自动跳过（判存在性）。
-- 脚本本身幂等，重复跑无副作用。
--
-- **askdb_ro 不在回滚范围内。** 它是内置数据源那条链路的钥匙，与本次改动
-- 无关；连它一起删会把 2026-08-12 那套只读接入也拆掉。
--
-- ⚠️ 回滚库侧之前，先把 askdb 侧回滚掉：撤掉这些角色而 askdb_sources 表里
--    还留着指向它们的数据源，站点上两张卡片会变成"连不上"，而不是消失。
--    顺序是：删数据源（DELETE /api/sources/{sid}）→ 回滚配置 → 再跑本脚本。

\set ON_ERROR_STOP on

-- ---- careermate_db 段 ----
DO $$
BEGIN
  IF current_database() = 'careermate_db'
     AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careermate_ro') THEN
    EXECUTE 'DROP OWNED BY careermate_ro';
    EXECUTE 'DROP ROLE careermate_ro';
    RAISE NOTICE 'careermate_ro 已删除';
  END IF;
END $$;

-- ---- ragforge 段 ----
-- DROP OWNED BY 会把 askdb_all_orgs 那 5 条策略一并带走；
-- askdb_org_isolation 属于 askdb_ro，不受影响。
DO $$
BEGIN
  IF current_database() = 'ragforge'
     AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ragforge_ro_all') THEN
    EXECUTE 'DROP OWNED BY ragforge_ro_all';
    EXECUTE 'DROP ROLE ragforge_ro_all';
    RAISE NOTICE 'ragforge_ro_all 已删除，askdb_ro 未动';
  END IF;
END $$;

-- ---- 元数据库段（连 postgres 库执行）----
-- **默认只删角色不删库。** askdb_sources 表里是已注册的数据源与表白名单，
-- 删库等于把它们一起烧掉，而回滚一次配置不该赔上这些。
-- 确实要连库一起删，手工执行下面那行（先断开所有连接）：
--   DROP DATABASE IF EXISTS askdb_meta;
DO $$
BEGIN
  IF current_database() = 'postgres'
     AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'askdb_meta') THEN
    RAISE NOTICE 'askdb_meta 角色保留 —— 它是 askdb_meta 库的属主，'
                 '删角色会连库一起废掉。确需清理时手工 DROP DATABASE 后再删角色。';
  END IF;
END $$;

SELECT '回滚段执行完毕，当前库：' || current_database() AS 结果;
