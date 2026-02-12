-- ============================================================
-- 数据库迁移脚本：添加 is_updated 列和索引
-- 用于支持三种下载模式和增量字段提取
-- 执行时间：2026-02-12
-- ============================================================

-- 1. 为 episode 表添加 is_updated 列
ALTER TABLE episode 
ADD COLUMN IF NOT EXISTS is_updated boolean NOT NULL DEFAULT false;

-- 2. 添加注释
COMMENT ON COLUMN episode.is_updated IS '标记该 episode 是否在本次数据收集中被更新，用于增量字段提取。每次数据收集开始前会被初始化为 false。';

-- 3. 创建部分索引（只索引被更新的行，节省空间）
CREATE INDEX IF NOT EXISTS idx_episode_updated 
ON episode(device_id, is_updated) 
WHERE is_updated = true;

-- 4. 验证变更
SELECT 
    column_name, 
    data_type, 
    is_nullable, 
    column_default
FROM information_schema.columns 
WHERE table_name = 'episode' 
  AND column_name = 'is_updated';

-- 5. 验证索引
SELECT 
    indexname,
    indexdef
FROM pg_indexes 
WHERE tablename = 'episode' 
  AND indexname = 'idx_episode_updated';

-- 完成提示
SELECT 'Migration completed successfully!' AS status;
