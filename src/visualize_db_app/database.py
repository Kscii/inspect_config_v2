# src/visualize_db_app/database.py
"""
数据库连接和查询模块
"""

from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
import pandas as pd


def qident(name: str) -> str:
    """SQL 标识符引用函数（用于 schema/table/column 名，不是 value 参数）"""
    return f'"{name}"'


def _placeholders(n: int) -> str:
    return ",".join(["%s"] * n)


class DatabaseManager:
    """数据库管理器"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._engine: Optional[Engine] = None

    def connect(self) -> Engine:
        """建立数据库连接"""
        if self._engine is None:
            db_url = (
                f"postgresql://{self.config['user']}:{self.config['password']}"
                f"@{self.config['host']}:{self.config['port']}/{self.config['database']}"
            )
            self._engine = create_engine(db_url, pool_pre_ping=True)
        return self._engine

    def close(self):
        """关闭数据库连接"""
        if self._engine:
            self._engine.dispose()
            self._engine = None

    def execute_query(self, query: str, params: Optional[Tuple] = None) -> pd.DataFrame:
        """执行查询并返回 DataFrame（支持 %s 参数）"""
        engine = self.connect()
        try:
            if params:
                param_dict = {f"param_{i}": val for i, val in enumerate(params)}
                query_modified = query
                for i in range(len(params)):
                    query_modified = query_modified.replace("%s", f":param_{i}", 1)
                return pd.read_sql_query(text(query_modified), engine, params=param_dict)
            else:
                return pd.read_sql_query(text(query), engine)
        except Exception as e:
            print(f"Query error: {e}")
            raise

    def get_models(self) -> List[str]:
        """获取所有 model（从 information_schema 读取 schema 名）"""
        query = """
        SELECT schema_name 
        FROM information_schema.schemata
        WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'public')
          AND schema_name NOT LIKE 'pg_%'
        ORDER BY schema_name
        """
        df = self.execute_query(query)
        return df["schema_name"].tolist()

    def get_rule_codes(self, model: str) -> List[str]:
        """获取指定 model 下的所有 rule_code"""
        schema = qident(model)
        query = f"""
        SELECT DISTINCT rule_code
        FROM {schema}.field
        WHERE rule_code IS NOT NULL AND rule_code != ''
        ORDER BY rule_code
        """
        df = self.execute_query(query)
        return df["rule_code"].tolist()

    def get_fields(self, model: str, rule_code: str) -> List[Dict[str, Any]]:
        """获取指定 model 和 rule_code 下的所有字段（按 field 排序）"""
        schema = qident(model)
        query = f"""
        SELECT field_id, field, field_name, type
        FROM {schema}.field
        WHERE rule_code = %s
        ORDER BY field
        """
        df = self.execute_query(query, (rule_code,))
        return df.to_dict("records")

    def get_field_names_batch(self, model: str, field_ids: List[int]) -> Dict[int, str]:
        """批量获取 field_id -> field_name（如果 field_name 为空则返回 field）"""
        if not field_ids:
            return {}
        schema = qident(model)
        ph = _placeholders(len(field_ids))
        query = f"""
        SELECT field_id, field, field_name
        FROM {schema}.field
        WHERE field_id IN ({ph})
        """
        df = self.execute_query(query, tuple(field_ids))
        if df.empty:
            return {}
        return {int(r["field_id"]): (r["field_name"] if r["field_name"] and r["field_name"].strip() else r["field"]) for _, r in df.iterrows()}

    def get_fields_batch(self, model: str, field_ids: List[int]) -> Dict[int, str]:
        """批量获取 field_id -> field"""
        if not field_ids:
            return {}
        schema = qident(model)
        ph = _placeholders(len(field_ids))
        query = f"""
        SELECT field_id, field
        FROM {schema}.field
        WHERE field_id IN ({ph})
        """
        df = self.execute_query(query, tuple(field_ids))
        if df.empty:
            return {}
        return {int(r["field_id"]): r["field"] for _, r in df.iterrows()}

    def get_field_info_batch(self, model: str, field_ids: List[int]) -> Dict[int, Dict[str, str]]:
        """批量获取 field_id -> {field, field_name, display_name, type}"""
        if not field_ids:
            return {}
        schema = qident(model)
        ph = _placeholders(len(field_ids))
        query = f"""
        SELECT field_id, field, field_name, type
        FROM {schema}.field
        WHERE field_id IN ({ph})
        """
        df = self.execute_query(query, tuple(field_ids))
        if df.empty:
            return {}
        result = {}
        for _, r in df.iterrows():
            fid = int(r["field_id"])
            field = r["field"]
            field_name = r["field_name"]
            field_type = r["type"]
            display_name = field_name if field_name and field_name.strip() else field
            result[fid] = {
                "field": field,
                "field_name": field_name,
                "display_name": display_name,
                "type": field_type,
            }
        return result

    def get_thresholds_batch(self, model: str, fields: List[str]) -> Dict[str, Optional[Dict[str, Dict[str, float]]]]:
        """批量获取阈值：field -> {base:{min,max}, full:{min,max}}；没有则返回 None"""
        if not fields:
            return {}
        schema = qident(model)
        ph = _placeholders(len(fields))

        # base
        query_base = f"""
        SELECT field, min, max
        FROM {schema}.thresholds_base
        WHERE field IN ({ph})
        """
        df_base = self.execute_query(query_base, tuple(fields))

        # full
        query_full = f"""
        SELECT field, min, max
        FROM {schema}.thresholds_full
        WHERE field IN ({ph})
        """
        df_full = self.execute_query(query_full, tuple(fields))

        result: Dict[str, Dict[str, Optional[Dict[str, float]]]] = {f: {"base": None, "full": None} for f in fields}

        if not df_base.empty:
            for _, row in df_base.iterrows():
                f = row["field"]
                try:
                    result[f]["base"] = {
                        "min": float(row["min"]) if pd.notna(row["min"]) else None,
                        "max": float(row["max"]) if pd.notna(row["max"]) else None,
                    }
                except (ValueError, TypeError):
                    result[f]["base"] = None

        if not df_full.empty:
            for _, row in df_full.iterrows():
                f = row["field"]
                try:
                    result[f]["full"] = {
                        "min": float(row["min"]) if pd.notna(row["min"]) else None,
                        "max": float(row["max"]) if pd.notna(row["max"]) else None,
                    }
                except (ValueError, TypeError):
                    result[f]["full"] = None

        # 归一：如果 base/full 都 None，则整体 None
        out: Dict[str, Optional[Dict[str, Dict[str, float]]]] = {}
        for f, v in result.items():
            if v["base"] is None and v["full"] is None:
                out[f] = None
            else:
                out[f] = {"base": v["base"], "full": v["full"]}
        return out

    def get_field_data(
        self,
        model: str,
        field_ids: List[int],
        time_range: Optional[Tuple[str, str]] = None,
    ) -> pd.DataFrame:
        """获取字段数据（包含 episode 维信息）"""
        schema = qident(model)

        where_clauses = [f"fv.field_id IN ({','.join(map(str, field_ids))})"]
        params: List[Any] = []

        if time_range:
            where_clauses.append("e.collected_at >= %s AND e.collected_at <= %s")
            params.extend(time_range)

        where_str = " AND ".join(where_clauses)

        query = f"""
        SELECT 
            fv.episode_id,
            fv.field_id,
            fv.value,
            e.sn,
            e.taskid,
            e.area,
            e.collected_at
        FROM {schema}.field_value fv
        JOIN {schema}.episode e ON fv.episode_id = e.episode_id
        WHERE {where_str}
        ORDER BY e.collected_at, fv.field_id
        """
        return self.execute_query(query, tuple(params) if params else None)

    def get_field_name(self, model: str, field_id: int) -> str:
        """兼容旧接口：单个 field_id 获取 field"""
        schema = qident(model)
        query = f"""
        SELECT field
        FROM {schema}.field
        WHERE field_id = %s
        """
        df = self.execute_query(query, (field_id,))
        if df.empty:
            return f"field_{field_id}"
        return df.iloc[0]["field"]

    def get_collect_json(self, model: str, episode_id: str) -> Optional[Dict[str, Any]]:
        """获取指定 episode 的 collect_json 数据"""
        schema = qident(model)
        query = f"""
        SELECT episode_id, filename, json
        FROM {schema}.collect_json
        WHERE episode_id = %s
        """
        df = self.execute_query(query, (episode_id,))
        if df.empty:
            return None
        row = df.iloc[0]
        return {
            "episode_id": row["episode_id"],
            "filename": row["filename"],
            "json": row["json"]
        }
