-- ============================================================
-- Inspect Config - 数据质量报告数据库（DDL）
-- ============================================================

-- 1) 建议使用 pgcrypto 生成 UUID（也可由应用层生成）
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- presets：环境/区域/OBS 配置（一个 prefix 对应一个 presets_id）
-- ============================================================
CREATE TABLE IF NOT EXISTS presets (
  presets_id            text PRIMARY KEY,
  area                  text NOT NULL,
  environment           text NOT NULL,

  -- 用于下载 report 的 OBS 位置信息
  report_obs_bucket     text NOT NULL,
  report_obs_path       text NOT NULL,

  -- 用于下载 rule 的 OBS 位置信息
  rule_obs_bucket       text NOT NULL,
  rule_obs_path         text NOT NULL,

  -- 用于调用 obsutil 的本地配置文件路径（不同 presets 不同）
  obsutil_config_path   text NOT NULL,

  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),

  -- area + environment 一般应唯一；如果你未来会扩展同环境多套 bucket，
  -- 可以移除这个 unique，仅以 presets_id 为准
  CONSTRAINT uq_presets_area_env UNIQUE (area, environment)
);

COMMENT ON TABLE presets IS '采集环境预设：一个 prefix 映射到一个 presets_id，包含 area/environment 及 obsutil 配置与 report/rule 的 bucket/path。';
COMMENT ON COLUMN presets.presets_id IS '主键：由 area + environment（以及你业务规则）决定的字符串 ID。';
COMMENT ON COLUMN presets.report_obs_bucket IS '用于下载 report 的 bucket（obs://bucket）。';
COMMENT ON COLUMN presets.report_obs_path IS '用于下载 report 的 path 前缀（bucket 内路径前缀）。';
COMMENT ON COLUMN presets.rule_obs_bucket IS '用于下载 rule 的 bucket（obs://bucket）。';
COMMENT ON COLUMN presets.rule_obs_path IS '用于下载 rule 的 path 前缀（bucket 内路径前缀，格式：range/{model}/{base/full}/{time}/{time}_range_{base/full}.csv）。';
COMMENT ON COLUMN presets.obsutil_config_path IS 'obsutil 配置文件路径（本机文件），用于指定 AK/SK/endpoint 等。';

-- ============================================================
-- device：设备维表（device_id 为 UUID；sn 在不同 area/model 可能重名）
-- ============================================================
CREATE TABLE IF NOT EXISTS device (
  device_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  presets_id    text NOT NULL REFERENCES presets(presets_id) ON DELETE RESTRICT,

  sn            text NOT NULL,
  area          text NOT NULL,
  model         text NOT NULL,

  -- 用于构造 post 请求的 id（如果未来不用可为空）
  api_map_id    text,

  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),

  -- 关键约束：同一 presets 下，同一 model+sn 视为同一设备
  CONSTRAINT uq_device_identity UNIQUE (presets_id, model, sn)
);

COMMENT ON TABLE device IS '设备表：同一 presets_id 下 (model,sn) 唯一。sn 在不同 area/model 可能重复，因此不能单独做唯一键。';
COMMENT ON COLUMN device.device_id IS '设备主键 UUID（推荐 DB 生成或应用层生成）。';
COMMENT ON COLUMN device.presets_id IS '设备归属的 presets（决定从哪个区域/环境采集与下载）。';
COMMENT ON COLUMN device.sn IS '设备序列号（可能跨 area/model 重名）。';
COMMENT ON COLUMN device.model IS '机器人型号（如 A2D、UR 等）。';
COMMENT ON COLUMN device.area IS '区域（建议与 presets.area 一致；写冗余是为了查询方便）。';

CREATE INDEX IF NOT EXISTS idx_device_presets ON device(presets_id);
CREATE INDEX IF NOT EXISTS idx_device_model ON device(model);

-- ============================================================
-- task：任务维表（一个 task 内 presets/device 不会变化）
-- task_id 从 OBS key 解析得到：collect/<task_id>/<episode_id>/...（你已确认）
-- ============================================================
CREATE TABLE IF NOT EXISTS task (
  task_id       text PRIMARY KEY,
  presets_id    text NOT NULL REFERENCES presets(presets_id) ON DELETE RESTRICT,
  device_id     uuid NOT NULL REFERENCES device(device_id) ON DELETE RESTRICT,

  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE task IS '任务表：一个 task_id 固定对应一个 presets_id 和 device_id（后续 episode 都引用它）。';
COMMENT ON COLUMN task.task_id IS '任务 ID（从 OBS 路径解析，类型为 text）。';
COMMENT ON COLUMN task.presets_id IS '该任务归属 presets（prefix->presets_id）。';
COMMENT ON COLUMN task.device_id IS '该任务固定的设备。';

CREATE INDEX IF NOT EXISTS idx_task_device ON task(device_id);
CREATE INDEX IF NOT EXISTS idx_task_presets ON task(presets_id);

-- ============================================================
-- episode：每条数据的“事实表”（以 episode_id 为唯一 ID）
-- collect_at：用于 Dash 时间过滤的主时间字段（你明确要求）
-- download_at：入库时间，用于运维/追踪
-- ============================================================
CREATE TABLE IF NOT EXISTS episode (
  episode_id    text PRIMARY KEY,

  task_id       text NOT NULL REFERENCES task(task_id) ON DELETE RESTRICT,
  device_id     uuid NOT NULL REFERENCES device(device_id) ON DELETE RESTRICT,

  collect_at    timestamptz NOT NULL,
  download_at   timestamptz NOT NULL DEFAULT now(),

  -- 标记该 episode 是否在本次数据收集中被更新（新下载或覆盖）
  is_updated    boolean NOT NULL DEFAULT false,

  -- 可选：如果你想追踪 report 顶层 createdAt，可另加 report_created_at
  -- report_created_at timestamptz,

  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE episode IS 'episode 事实表：每个 episode_id 一条记录，关联 task/device，并以 collect_at 作为时间筛选主字段。';
COMMENT ON COLUMN episode.collect_at IS '采集时间：建议取 metadata.json 中的 collected_at（优先于 report 顶层 createdAt）。用于 Dash 时间过滤。';
COMMENT ON COLUMN episode.download_at IS '入库时间：该 report 被 ingest 写入数据库的时间（now）。';
COMMENT ON COLUMN episode.is_updated IS '标记该 episode 是否在本次数据收集中被更新，用于增量字段提取。每次数据收集开始前会被初始化为 false。';
COMMENT ON COLUMN episode.task_id IS '所属任务，来自 OBS key 解析（collect/<task_id>/<episode_id>/...）。';
COMMENT ON COLUMN episode.device_id IS '所属设备（冗余字段，便于查询；理论上应与 task.device_id 一致，写入时务必保证一致）。';

-- Dash 常用过滤索引
CREATE INDEX IF NOT EXISTS idx_episode_collect_at ON episode(collect_at);
CREATE INDEX IF NOT EXISTS idx_episode_task_id ON episode(task_id);
CREATE INDEX IF NOT EXISTS idx_episode_device_id ON episode(device_id);

-- 部分索引：用于字段提取时快速查询被更新的 episode
CREATE INDEX IF NOT EXISTS idx_episode_updated ON episode(device_id, is_updated) WHERE is_updated = true;

-- ============================================================
-- json_report：原始 JSON 报告存档（用于追溯/预览/重新解析）
-- ============================================================
CREATE TABLE IF NOT EXISTS json_report (
  episode_id                 text PRIMARY KEY REFERENCES episode(episode_id) ON DELETE CASCADE,

  file_name                  text NOT NULL,
  json_report_ver            text,
  json_report                jsonb NOT NULL,

  -- 从 obsutil ls 得到的完整路径：obs://bucket/key（你已确认要存）
  json_report_download_path  text NOT NULL,

  created_at                 timestamptz NOT NULL DEFAULT now(),
  updated_at                 timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT uq_json_report_download_path UNIQUE (json_report_download_path)
);

COMMENT ON TABLE json_report IS '原始报告 JSON 存档：用于预览/追溯/格式变化时重算字段。分析查询尽量不要直接扫 jsonb。';
COMMENT ON COLUMN json_report.file_name IS '报告文件名，一般为 {episode_id}_collect.json。';
COMMENT ON COLUMN json_report.json_report_download_path IS 'obsutil list 得到的完整 obs://bucket/key，可直接用于下载该报告。';

-- 查询时通常不会用到，但用于去重和追踪
CREATE INDEX IF NOT EXISTS idx_json_report_ver ON json_report(json_report_ver);

-- ============================================================
-- rule：规则配置（同 device_id + presets_id + rule_type 对应一套 rule）
-- 你当前规则文件名是创建时间 → ingest 时选择“最新” rule
-- ============================================================
CREATE TABLE IF NOT EXISTS rule (
  rule_id        bigserial PRIMARY KEY,

  presets_id     text NOT NULL REFERENCES presets(presets_id) ON DELETE RESTRICT,
  device_id      uuid NOT NULL REFERENCES device(device_id) ON DELETE RESTRICT,

  -- base/full 两套
  rule_type      text NOT NULL CHECK (rule_type IN ('base', 'full')),

  -- rule 文本（或摘要），用于快速展示/调试
  csv_rule_txt   text,

  rule_obs_bucket  text NOT NULL,
  rule_obs_path    text NOT NULL,

  -- 规则版本/文件名（你说文件名是创建时间，建议也存下来）
  rule_file_name   text,
  rule_file_time   timestamptz,

  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),

  -- 同一设备同一类型只应有“当前生效的一条”
  -- 如果你想保留历史规则，可以：
  -- 1) 去掉这个 unique
  -- 2) 增加 is_active + 生效时间段
  CONSTRAINT uq_rule_current UNIQUE (presets_id, device_id, rule_type)
);

COMMENT ON TABLE rule IS '规则配置：每个 (presets_id,device_id,rule_type) 一套“当前生效”规则；如要保留历史需扩展版本化设计。';
COMMENT ON COLUMN rule.rule_type IS '规则类型：base/full。';
COMMENT ON COLUMN rule.rule_obs_path IS '规则文件所在路径前缀（通常由系统服务传入/obs list 得到）。';
COMMENT ON COLUMN rule.rule_file_name IS '规则文件名（你目前约定为创建时间命名）。';

CREATE INDEX IF NOT EXISTS idx_rule_device ON rule(device_id);
CREATE INDEX IF NOT EXISTS idx_rule_presets ON rule(presets_id);

-- ============================================================
-- field：字段维表（扁平化后的“指标定义”）
-- 约束：一个 field 永远只属于一个 rule_code（你已确认 4b）
-- ============================================================
CREATE TABLE IF NOT EXISTS field (
  field_id      bigserial PRIMARY KEY,

  -- 稳定唯一定位键：建议规则：
  -- field_key = "{rule_code}::{unit_name}::{json_path}"
  field_key     text NOT NULL,

  -- 阅读友好名称（不要求唯一）
  field_name    text NOT NULL,

  -- 指标归属 rule（如 frame_count/frame_fit/...）
  rule_code     text NOT NULL,

  -- 叶子字段名（用于聚类/对比）：例如 fps/score/jitter_std_ms/误差均值
  field_type    text NOT NULL,

  -- numeric / non_numeric 决定入哪张 value 表
  data_type     text NOT NULL CHECK (data_type IN ('numeric', 'non_numeric')),

  -- 指标所属 model（不同 model 的字段结构不同）
  model         text NOT NULL,

  -- selector 阶段选择：只有 is_selected=true 的字段需要绘图（可大幅减轻 Dash 压力）
  is_selected   boolean NOT NULL DEFAULT false,

  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT uq_field_model_key UNIQUE (model, field_key)
);

COMMENT ON TABLE field IS '字段定义表：从 report JSON 扁平化得到的“指标定义”。同一 model 下 field_key 唯一。';
COMMENT ON COLUMN field.field_key IS '稳定唯一键：推荐 {rule_code}::{rawDataMetric.name}::{json_path}，用于长期可复现定位。';
COMMENT ON COLUMN field.rule_code IS '所属规则码：field 永远只属于一个固定 rule_code（后续只会新增字段，不会迁移）。';
COMMENT ON COLUMN field.field_type IS '叶子字段名（分类用）。';
COMMENT ON COLUMN field.data_type IS 'numeric/non_numeric：决定写入 field_value_num 或 field_value_text。';
COMMENT ON COLUMN field.is_selected IS '是否被 selector 选中：Dash 默认只查询 is_selected=true 的字段。';

CREATE INDEX IF NOT EXISTS idx_field_rule_model ON field(rule_code, model);
CREATE INDEX IF NOT EXISTS idx_field_model ON field(model);
-- 选择字段查询高频：建议部分索引
CREATE INDEX IF NOT EXISTS idx_field_selected_true ON field(field_id) WHERE is_selected = true;

-- ============================================================
-- field_value_num：数值型指标
-- 注意：value 使用 numeric（你已要求）
-- ============================================================
CREATE TABLE IF NOT EXISTS field_value_num (
  field_num_value_id  bigserial PRIMARY KEY,

  episode_id          text NOT NULL REFERENCES episode(episode_id) ON DELETE CASCADE,
  field_id            bigint NOT NULL REFERENCES field(field_id) ON DELETE CASCADE,

  value               numeric NOT NULL,

  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT uq_field_value_num UNIQUE (episode_id, field_id)
);

COMMENT ON TABLE field_value_num IS '数值型指标值表：每个 (episode_id,field_id) 至多一条。';
COMMENT ON COLUMN field_value_num.value IS '数值型指标统一用 numeric，避免 float 精度与比较问题（代价是写入略慢）。';

CREATE INDEX IF NOT EXISTS idx_fvn_field_id ON field_value_num(field_id);
CREATE INDEX IF NOT EXISTS idx_fvn_episode_id ON field_value_num(episode_id);

-- ============================================================
-- field_value_text：非数值指标（字符串/JSON 串/错误信息等）
-- ============================================================
CREATE TABLE IF NOT EXISTS field_value_text (
  field_text_value_id  bigserial PRIMARY KEY,

  episode_id           text NOT NULL REFERENCES episode(episode_id) ON DELETE CASCADE,
  field_id             bigint NOT NULL REFERENCES field(field_id) ON DELETE CASCADE,

  value                text NOT NULL,

  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT uq_field_value_text UNIQUE (episode_id, field_id)
);

COMMENT ON TABLE field_value_text IS '非数值型指标值表：存字符串、错误信息、或扁平化后无法合理拆分的 JSON 串。';
CREATE INDEX IF NOT EXISTS idx_fvt_field_id ON field_value_text(field_id);
CREATE INDEX IF NOT EXISTS idx_fvt_episode_id ON field_value_text(episode_id);

-- ============================================================
-- field_range：规则阈值（按 rule_id + field_id 存 min/max）
-- ============================================================
CREATE TABLE IF NOT EXISTS field_range (
  field_id     bigint NOT NULL REFERENCES field(field_id) ON DELETE CASCADE,
  rule_id      bigint NOT NULL REFERENCES rule(rule_id) ON DELETE CASCADE,

  min_range    numeric,
  max_range    numeric,

  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),

  PRIMARY KEY (field_id, rule_id)
);

COMMENT ON TABLE field_range IS '字段阈值表：每个规则(rule_id)对每个字段(field_id)给出 min/max（numeric）。';
COMMENT ON COLUMN field_range.min_range IS '允许范围下界（可空：表示不约束）。';
COMMENT ON COLUMN field_range.max_range IS '允许范围上界（可空：表示不约束）。';

CREATE INDEX IF NOT EXISTS idx_field_range_rule ON field_range(rule_id);

-- ============================================================
-- ingest_run：每次全量 list / 增量入库的运行记录（强烈建议）
-- ============================================================
CREATE TABLE IF NOT EXISTS ingest_run (
  run_id        bigserial PRIMARY KEY,
  presets_id    text NOT NULL REFERENCES presets(presets_id) ON DELETE RESTRICT,

  -- 运行窗口（便于排查）
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,

  listed_count  bigint NOT NULL DEFAULT 0,
  missing_count bigint NOT NULL DEFAULT 0,
  downloaded_count bigint NOT NULL DEFAULT 0,
  inserted_episode_count bigint NOT NULL DEFAULT 0,

  success       boolean NOT NULL DEFAULT false,
  error_message text,

  created_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE ingest_run IS '入库运行记录：每轮全量 list / 更新的统计与错误信息，便于云端运维。';
CREATE INDEX IF NOT EXISTS idx_ingest_run_presets_started ON ingest_run(presets_id, started_at DESC);
