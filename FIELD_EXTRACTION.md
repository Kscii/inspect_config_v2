# 字段提取功能使用说明

## 功能概述

字段提取功能可以从已入库的 `json_report` 数据中：
1. 自动生成字段定义（selector）
2. 提取字段值
3. 根据数据类型分别存储到 `field_value_num` 或 `field_value_text` 表

## 使用方式

### 方式1：独立命令执行（推荐）

```bash
# 1. 先完成数据收集
python -m src.main --presets shanghai_prod

# 2. 提取所有 model 的字段
python -m src.main --extract-fields

# 3. 提取指定 model 的字段
python -m src.main --extract-fields --model A2D

# 4. 测试模式（限制处理数量）
python -m src.main --extract-fields --model A2D --limit 100
```

### 方式2：配置文件启用（自动执行）

修改 `config/config.yaml`：

```yaml
ingest:
  enable_field_extraction: true  # 改为 true
  field_batch_size: 1000
```

然后运行采集命令时会自动提取字段：

```bash
python -m src.main --presets shanghai_prod
```

## 实现细节

### 两遍扫描策略

**阶段1：收集字段定义**
- 扫描所有 episode 的 JSON
- 生成唯一的 selector 列表
- 提取字段元数据（name, rule_code, type）
- 判断数据类型（numeric/non_numeric）
- 批量插入到 `field` 表

**阶段2：提取字段值**
- 再次扫描所有 episode
- 使用已建立的 field_id 映射
- 提取每个字段的值
- 批量插入到 `field_value_num` 或 `field_value_text`

### 字段定义规则

**field_key**: 直接使用 selector
```
例: .<rawDataMetricList>.[<ruleCode>=<frame_count>].<score>
```

**rule_code**: 从 `[<ruleCode>=<xxx>]` 提取
```
例: .<rawDataMetricList>.[<ruleCode>=<frame_count>].<score>
    → rule_code = "frame_count"
```

**field_type**: 取最后一个 `.<xxx>` 中的内容
```
例: .<rawDataMetricList>.[<ruleCode>=<frame_count>].<score>
    → field_type = "score"
```

**field_name**: 复杂规则（参考上一个项目）
- 对于 `metadata_raw` 规则：特殊处理
- 其他：取 `[<name>=<xxx>]` 和 `.<yyy>` 拼接
```
例: frame_count-score
```

### 数据类型判断

**non_numeric** 的情况：
1. rule_code 为 `metadata_raw`
2. bool 值（true/false）
3. None 值
4. list 类型
5. 空字符串 `""`
6. 字符串无法转换为数字

**numeric** 的情况：
- int, float 数字类型
- 字符串 "null"（特殊）
- 可转换为数字的字符串

### 性能优化

- **批量插入**: 使用 `execute_values` 批量操作
- **字段缓存**: 第一遍扫描后建立 field_key → field_id 映射
- **批量缓冲**: 累积到 `field_batch_size` 条后再提交
- **进度日志**: 每 1000 条记录输出进度

## 性能评估

对于 24万 episode × 100 字段 = 2400万 条记录：

**预计耗时**：
- 阶段1（字段定义）：5-10 分钟
- 阶段2（字段值）：30-60 分钟
- 总计：约 40-70 分钟

**数据库影响**：
- field 表：约 100-200 行（按 model）
- field_value_num: 约 1500-2000万 行
- field_value_text: 约 400-500万 行

## 注意事项

1. **首次运行建议测试**：先用 `--limit 100` 测试
2. **数据量大时谨慎开启自动提取**：建议使用独立命令
3. **数据库空间**：确保有足够磁盘空间存储字段值
4. **is_selected 默认全部 true**：后续可通过 SQL 更新筛选

## 示例输出

```
============================================================
开始字段提取: model=A2D
============================================================
[字段提取] 阶段1: 收集字段定义...
  已扫描 1000 个 episode，发现 85 个唯一字段
  已扫描 2000 个 episode，发现 92 个唯一字段
  扫描完成：共 5000 个 episode，发现 95 个唯一字段
⏱️  字段收集完成: 95 个唯一字段，耗时 12.34秒

[字段提取] 批量插入字段定义到数据库...
⏱️  字段定义插入完成: 耗时 0.56秒

[字段提取] 阶段2: 提取字段值...
  已处理 1000 个 episode
  已处理 2000 个 episode
⏱️  批量提交 1000 条数据，耗时 0.23秒 (平均 0.2ms/条)
⏱️  字段值提取完成: 耗时 145.67秒

完成字段提取 A2D:
  - 扫描 episode: 5000
  - 创建字段: 95
  - 数值型字段值: 350000
  - 文本型字段值: 125000
  - 错误数: 0
⏱️  总耗时: 158.57秒 (2.6分钟)
```

## 故障排查

**Q: 提取失败怎么办？**
A: 检查日志文件 `logs/ingest.log`，查看具体错误信息

**Q: 字段值为空？**
A: 检查 JSON 结构是否与 selector 匹配，使用 `--log-level DEBUG` 查看详细日志

**Q: 数据类型判断错误？**
A: 可以修改 `field_parser.py` 中的 `determine_data_type` 函数调整规则

**Q: 内存占用过高？**
A: 降低 `field_batch_size` 配置（默认 1000）
