# Rule 和 Field_Range 表未写入问题分析报告

## 🔍 问题根因分析

经过详细检查代码，发现 **`range_importer.py`** 中存在多个严重的 API 调用错误，导致 Range 导入流程完全无法执行。

---

## ❌ 发现的问题

### 1. **ObsClient 初始化参数错误**

**位置**: `src/ingest/range_importer.py` 第 124-128 行

**错误代码**:
```python
obs_client = ObsClient(
    bucket=preset['rule_obs_bucket'],           # ❌ 错误参数
    path_prefix=preset['rule_obs_path'],        # ❌ 错误参数
    obsutil_config_path=preset['obsutil_config_path'],  # ❌ 参数名错误
    obsutil_exe=self.obsutil_exe
)
```

**正确的参数**:
```python
obs_client = ObsClient(
    config_path=preset['obsutil_config_path'],  # ✅ 正确参数
    obsutil_exe=self.obsutil_exe
)
```

**影响**: ObsClient 对象根本无法创建，导致整个 Range 导入流程在第一步就失败。

---

### 2. **list_files() 方法调用错误**

**位置**: `src/ingest/range_importer.py` 第 133 行

**错误代码**:
```python
all_files = obs_client.list_files(limit=self.config.ingest.get('obs_list_limit', 10000))
```

**问题**: 
- 缺少必需的 `bucket` 和 `path` 参数
- `list_files()` 返回的是 `List[Dict]`（包含 path 和 last_modified），但代码期望 `List[str]`

**正确代码**:
```python
all_files = obs_client.list_files(
    bucket=preset['rule_obs_bucket'],
    path=preset['rule_obs_path'],
    limit=self.config.ingest.get('obs_list_limit', 10000),
    max_total=self.config.ingest.get('max_total_files', 50000)
)
# 从返回的字典中提取 path
range_files = [f['path'] for f in all_files if 'range/' in f['path'] and f['path'].endswith('.csv')]
```

---

### 3. **download_file() 方法调用错误**

**位置**: `src/ingest/range_importer.py` 第 246 行

**错误代码**:
```python
obs_client.download_file(obs_key, str(local_path))
```

**问题**:
- `obs_key` 可能只是相对路径，没有 `obs://` 前缀
- `download_file()` 期望完整的 `obs://bucket/path` 格式
- 返回的 `all_files` 已经是完整的 `obs://` 格式，但代码混淆了路径格式

**正确代码**:
```python
# obs_key 已经是完整的 obs://bucket/path 格式
obs_client.download_file(obs_key, local_path)
```

---

### 4. **路径解析不支持 obs:// 前缀**

**位置**: `src/ingest/range_parser.py` 第 15-30 行

**问题**: `RangeParser.parse_range_path()` 无法处理带 `obs://bucket/` 前缀的路径

**修复**: 添加了前缀去除逻辑
```python
# 去掉 obs:// 前缀和 bucket 名称
path_str = obs_key
if path_str.startswith('obs://'):
    # obs://bucket/range/... -> range/...
    path_str = '/'.join(path_str.split('/')[3:])
```

---

## ✅ 已修复的问题

所有上述问题已在以下文件中修复：

1. ✅ **src/ingest/range_importer.py**
   - 修复 ObsClient 初始化参数
   - 修复 list_files() 调用
   - 修复 download_file() 调用
   - 修复路径处理逻辑

2. ✅ **src/ingest/range_parser.py**
   - 添加 obs:// 前缀处理

---

## 🔄 代码执行流程（修复后）

1. **创建 ObsClient** → 使用正确的 config_path 参数
2. **列举文件** → 传入 bucket 和 path，返回带时间戳的文件列表
3. **过滤 range 文件** → 从字典中提取 path
4. **分组选择最新** → 按 model 和 rule_type 分组
5. **下载文件** → 使用完整的 obs:// 路径
6. **解析 CSV** → 提取 field、min、max
7. **创建 rule** → 调用 repository.upsert_rule()
8. **匹配字段** → 调用 repository.batch_get_field_ids()
9. **插入 field_range** → 调用 repository.batch_insert_field_ranges()

---

## 📊 预期结果

修复后，Range 导入流程应该能够：

1. ✅ 成功列举 OBS 上的 range CSV 文件
2. ✅ 下载并解析 CSV 内容
3. ✅ 在 `rule` 表中创建规则记录
4. ✅ 在 `field_range` 表中插入字段范围数据
5. ✅ 输出详细的统计日志

---

## 🚀 验证步骤

1. 运行数据采集流程：
```bash
python -m src.main
```

2. 检查日志输出：
```
开始自动导入 Range 规则...
处理 Preset: xxx
列举 OBS 文件: obs://bucket/path
发现 X 个 range CSV 文件
选择最新规则: model/base -> ...
导入规则: preset/model/base
  下载到: /tmp/...
  解析 CSV...
  解析到 X 条字段范围
  创建/更新规则...
  规则ID: xxx
  匹配字段...
  插入 X 条字段范围...
  ✓ 总数=X, 插入=X, 更新=0, 跳过=0
```

3. 查询数据库验证：
```sql
-- 检查 rule 表
SELECT COUNT(*) FROM rule;
SELECT * FROM rule ORDER BY created_at DESC LIMIT 5;

-- 检查 field_range 表
SELECT COUNT(*) FROM field_range;
SELECT fr.*, f.field_key, r.rule_type 
FROM field_range fr
JOIN field f ON fr.field_id = f.field_id
JOIN rule r ON fr.rule_id = r.rule_id
LIMIT 10;
```

---

## 📝 总结

**根本原因**: API 调用参数完全错误，导致 Range 导入流程在初始化阶段就失败。

**影响范围**: `rule` 和 `field_range` 表完全没有数据写入。

**修复状态**: ✅ 所有问题已修复，代码现在应该能正常工作。
