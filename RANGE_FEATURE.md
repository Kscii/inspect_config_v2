# Range 功能说明

## 概述

Range 功能用于从 OBS 读取字段范围配置文件，并将这些配置存储到数据库中。Range 文件定义了每个字段的最小值和最大值范围。

## OBS 路径结构

```
range/{model}/{base/full}/{time}/{time}_range_{base/full}.csv
```

### 路径组成

- `{model}`: 机器人型号（如 A2D, UR, QINGLOONGV2.5 等）
- `{base/full}`: 规则类型
  - `base`: 基础规则
  - `full`: 完整规则
- `{time}`: 时间戳，格式为 `YYYYMMDD_HHMMSS`（如 `20260209_131649`）
- 文件名格式: `{time}_range_{base/full}.csv`

### 示例路径

```
range/A2D/base/20260209_131649/20260209_131649_range_base.csv
range/UR/full/20260210_093045/20260210_093045_range_full.csv
range/QINGLOONGV2.5/base/20260211_160000/20260211_160000_range_base.csv
```

## CSV 文件格式

Range CSV 文件包含以下列：

```csv
field,min,max,pass_count,fail_count,pass_rate
```

### 列说明

- **field** (必需): 字段名称/路径
- **min** (必需): 字段最小值（可为空）
- **max** (必需): 字段最大值（可为空）
- **pass_count**: 通过次数（忽略）
- **fail_count**: 失败次数（忽略）
- **pass_rate**: 通过率（忽略）

**注意**: 系统只关心 `field`, `min`, `max` 三列，其他列会被忽略。

### CSV 示例

```csv
field,min,max,pass_count,fail_count,pass_rate
report.<modelInfo>.<left_arm>.[<name>=<position>],0.1,1.5,100,5,0.95
report.<modelInfo>.<right_arm>.[<name>=<position>],0.1,1.5,98,7,0.93
report.<modelInfo>.<left_arm>.[<name>=<velocity>],-2.0,2.0,95,10,0.90
```

## 数据库表结构

### presets 表（已更新）

```sql
CREATE TABLE presets (
  presets_id            text PRIMARY KEY,
  area                  text NOT NULL,
  environment           text NOT NULL,
  
  report_obs_bucket     text NOT NULL,
  report_obs_path       text NOT NULL,
  
  rule_obs_bucket       text NOT NULL,  -- 新增
  rule_obs_path         text NOT NULL,  -- 新增
  
  obsutil_config_path   text NOT NULL,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
```

### rule 表

```sql
CREATE TABLE rule (
  rule_id          bigserial PRIMARY KEY,
  presets_id       text NOT NULL REFERENCES presets(presets_id),
  device_id        uuid NOT NULL REFERENCES device(device_id),
  rule_type        text NOT NULL CHECK (rule_type IN ('base', 'full')),
  csv_rule_txt     text,
  rule_obs_bucket  text NOT NULL,
  rule_obs_path    text NOT NULL,
  rule_file_name   text,
  rule_file_time   timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  
  CONSTRAINT uq_rule_current UNIQUE (presets_id, device_id, rule_type)
);
```

### field_range 表

```sql
CREATE TABLE field_range (
  field_id     bigint NOT NULL REFERENCES field(field_id) ON DELETE CASCADE,
  rule_id      bigint NOT NULL REFERENCES rule(rule_id) ON DELETE CASCADE,
  min_range    numeric,
  max_range    numeric,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  
  PRIMARY KEY (field_id, rule_id)
);
```

## 配置文件

### config.yaml

```yaml
presets:
  - presets_id: shanghai_dev
    area: shanghai
    environment: dev
    report_obs_bucket: openloong-apps-dev-private
    report_obs_path: data-collector-svc/collect
    rule_obs_bucket: openloong-apps-dev-private
    rule_obs_path: data-collector-svc/range
    obsutil_config_path: ${OBSUTIL_CONFIG_SHANGHAI:/home/app-dev/.obsutil_shanghai/.obsutilconfig_shanghai}
```

## Python 模块

### RangeParser 类

位置: `src/ingest/range_parser.py`

#### 主要方法

1. **parse_range_path(obs_key: str)**: 解析 OBS 路径，提取元数据
2. **parse_range_csv(csv_path: Path)**: 解析 CSV 文件内容
3. **get_latest_range_file(file_list, model, rule_type)**: 获取最新的 range 文件

#### 使用示例

```python
from src.ingest import RangeParser

# 解析 OBS 路径
metadata = RangeParser.parse_range_path('range/A2D/base/20260209_131649/20260209_131649_range_base.csv')
# 返回: {'model': 'A2D', 'rule_type': 'base', 'time_str': '20260209_131649', 'datetime': datetime(...)}

# 解析 CSV 文件
ranges = RangeParser.parse_range_csv(Path('/tmp/range_base.csv'))
# 返回: [{'field': '...', 'min': 0.1, 'max': 1.5}, ...]

# 获取最新文件
latest_file, latest_time = RangeParser.get_latest_range_file(
    file_list=['range/A2D/base/20260209_131649/...', 'range/A2D/base/20260210_093045/...'],
    model='A2D',
    rule_type='base'
)
```

## 工作流程

1. **列举文件**: 使用 obsutil 列举指定 presets 的 `rule_obs_bucket` 和 `rule_obs_path` 下的所有 range 文件
2. **选择最新文件**: 对每个 (model, rule_type) 组合，选择时间戳最新的文件
3. **下载文件**: 使用 obsutil 下载选中的 CSV 文件
4. **解析内容**: 使用 RangeParser 解析 CSV 文件，提取 field, min, max
5. **存储到数据库**:
   - 创建或更新 `rule` 记录
   - 为每个字段创建或更新 `field_range` 记录

## 版本管理策略

当前设计使用 **最新文件覆盖** 策略:

- 同一 (presets_id, device_id, rule_type) 只保留一条当前生效的规则
- 通过 `UNIQUE` 约束: `CONSTRAINT uq_rule_current UNIQUE (presets_id, device_id, rule_type)`
- 更新时会覆盖旧规则

如需保留历史版本，可扩展设计:
- 添加 `is_active` 字段
- 添加生效时间段字段
- 移除或调整 UNIQUE 约束

## 注意事项

1. **时间格式**: 严格遵守 `YYYYMMDD_HHMMSS` 格式
2. **路径结构**: 必须完全符合 `range/{model}/{base/full}/{time}/{time}_range_{base/full}.csv` 格式
3. **CSV 编码**: 使用 UTF-8 编码
4. **数值处理**: min 和 max 可以为空（NULL），表示不限制该边界
5. **字段匹配**: field 字段需要与 report JSON 中的字段路径完全匹配
