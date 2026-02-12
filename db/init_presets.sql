-- ============================================================
-- Presets 表初始化数据
-- 基于 config.yaml 中的 global.presets 配置
-- ============================================================

-- 清空已有数据（可选：如果需要保留历史数据请注释掉）
-- TRUNCATE TABLE presets CASCADE;

-- ============================================================
-- 上海生产环境
-- ============================================================
INSERT INTO presets (
    presets_id,
    area,
    environment,
    report_obs_bucket,
    report_obs_path,
    rule_obs_bucket,
    rule_obs_path,
    obsutil_config_path
) VALUES (
    'shanghai_prod',
    'shanghai',
    'prod',
    'openloong-apps-prod-private',
    'data-collector-svc/collect',
    'openloong-apps-prod-private',
    'data-collector-svc/range',
    '/home/app-dev/.obsutil_shanghai/.obsutilconfig_shanghai'
) ON CONFLICT (presets_id) DO UPDATE SET
    area = EXCLUDED.area,
    environment = EXCLUDED.environment,
    report_obs_bucket = EXCLUDED.report_obs_bucket,
    report_obs_path = EXCLUDED.report_obs_path,
    rule_obs_bucket = EXCLUDED.rule_obs_bucket,
    rule_obs_path = EXCLUDED.rule_obs_path,
    obsutil_config_path = EXCLUDED.obsutil_config_path,
    updated_at = now();

-- ============================================================
-- 上海开发环境
-- ============================================================
INSERT INTO presets (
    presets_id,
    area,
    environment,
    report_obs_bucket,
    report_obs_path,
    rule_obs_bucket,
    rule_obs_path,
    obsutil_config_path
) VALUES (
    'shanghai_dev',
    'shanghai',
    'dev',
    'openloong-apps-dev-private',
    'data-collector-svc/collect',
    'openloong-apps-dev-private',
    'data-collector-svc/range',
    '/home/app-dev/.obsutil_shanghai/.obsutilconfig_shanghai'
) ON CONFLICT (presets_id) DO UPDATE SET
    area = EXCLUDED.area,
    environment = EXCLUDED.environment,
    report_obs_bucket = EXCLUDED.report_obs_bucket,
    report_obs_path = EXCLUDED.report_obs_path,
    rule_obs_bucket = EXCLUDED.rule_obs_bucket,
    rule_obs_path = EXCLUDED.rule_obs_path,
    obsutil_config_path = EXCLUDED.obsutil_config_path,
    updated_at = now();

-- ============================================================
-- 郑州生产环境
-- ============================================================
INSERT INTO presets (
    presets_id,
    area,
    environment,
    report_obs_bucket,
    report_obs_path,
    rule_obs_bucket,
    rule_obs_path,
    obsutil_config_path
) VALUES (
    'zhengzhou_prod',
    'zhengzhou',
    'prod',
    'openloong-zhengzhou-apps-private',
    'data-collector-svc/collect',
    'openloong-zhengzhou-apps-private',
    'data-collector-svc/range',
    '/home/app-dev/.obsutil_zhengzhou/.obsutilconfig_zhengzhou'
) ON CONFLICT (presets_id) DO UPDATE SET
    area = EXCLUDED.area,
    environment = EXCLUDED.environment,
    report_obs_bucket = EXCLUDED.report_obs_bucket,
    report_obs_path = EXCLUDED.report_obs_path,
    rule_obs_bucket = EXCLUDED.rule_obs_bucket,
    rule_obs_path = EXCLUDED.rule_obs_path,
    obsutil_config_path = EXCLUDED.obsutil_config_path,
    updated_at = now();

-- 查看插入结果
SELECT 
    presets_id,
    area,
    environment,
    report_obs_bucket,
    report_obs_path,
    rule_obs_bucket,
    rule_obs_path
FROM presets
ORDER BY area, environment;
