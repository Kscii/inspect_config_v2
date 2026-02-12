# 三种下载模式功能实现完成

## ✅ 实现完成的功能

### 1. 数据库变更
- ✅ `episode` 表新增 `is_updated` 列（boolean，默认 false）
- ✅ 添加部分索引 `idx_episode_updated`（只索引被更新的记录）
- ✅ 提供迁移脚本：`db/migration_add_is_updated.sql`

### 2. 配置文件更新
- ✅ `config.yaml` 中 `skip_existing` 改为 `download_mode`
- ✅ 支持三种模式：`new_only`、`incremental`、`full`

### 3. OBS 客户端增强
- ✅ `list_files()` 返回带 `last_modified` 时间戳的文件信息
- ✅ 解析 obsutil ls 输出中的时间戳信息

### 4. 数据仓库新增方法
- ✅ `reset_all_episode_update_flags()` - 全局初始化标记
- ✅ `get_last_successful_run()` - 查询上次成功运行
- ✅ `batch_insert_episodes()` - 重构支持三种模式
- ✅ `cleanup_updated_episode_field_values()` - 只清理被更新的 episode

### 5. 数据处理器实现三种模式
- ✅ **new_only**: 预过滤已存在的 episode，只下载新的
- ✅ **incremental**: 基于 LastModified 时间增量下载，可能覆盖
- ✅ **full**: 全量下载，覆盖所有

### 6. 字段提取优化
- ✅ 只提取被标记为 `is_updated=true` 的 episode
- ✅ 只清理被更新 episode 的字段值，保留 field 定义

### 7. 代码修复
- ✅ 修复类型导入（Dict, Tuple）
- ✅ 修复函数签名语法错误
- ✅ 用标准库 timezone 替代 pytz

## 📋 使用说明

### 执行数据库迁移
```bash
psql -h localhost -p 5433 -U inspect_user -d inspect_config_db -f db/migration_add_is_updated.sql
```

### 配置下载模式
在 `config/config.yaml` 中设置：
```yaml
ingest:
  download_mode: "new_only"  # 或 "incremental" 或 "full"
```

### 运行流程
```bash
python -m src.main
```

## 🔄 三种模式对比

| 模式 | 预过滤 | 覆盖行为 | 适用场景 |
|------|--------|----------|----------|
| **new_only** | 已存在的 episode | 不覆盖 | 日常增量采集 |
| **incremental** | LastModified < started_at | 覆盖更新的 | 文件可能被更新的场景 |
| **full** | 无过滤 | 全部覆盖 | 初始化或数据修复 |

## ⚙️ 实现细节

### 批量插入策略
- **保持 COPY 高性能**：继续使用临时表 + COPY
- **new_only**: `ON CONFLICT DO NOTHING`
- **incremental/full**: 先删除 json_report，再 `ON CONFLICT DO UPDATE`

### 字段提取优化
- 每次数据采集开始前全局重置 `is_updated = false`
- 批量插入时标记 `is_updated = true`
- 字段提取只查询 `WHERE is_updated = true`

### 性能保证
- ✅ 部分索引节省空间（只索引 true 值）
- ✅ 继续使用 COPY 保证批量插入速度
- ✅ 并发处理保持 23 线程
- ✅ 增量字段提取减少扫描量

## 🚀 下一步

系统已准备就绪，可以：
1. 执行数据库迁移脚本
2. 根据需要调整 download_mode
3. 开始新的数据采集任务
