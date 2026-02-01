# src/visualize_db_app/database.py
"""
数据库连接和查询模块
"""

from typing import Any, Dict, List, Optional, Tuple
import psycopg2
from psycopg2.extras import RealDictCursor
import pandas as pd


def qident(name: str) -> str:
    """SQL 标识符引用函数（用于 schema/table/column 名，不是 value 参数）"""
    return f'"{name}"'


class DatabaseManager:
    """数据库管理器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._conn = None
    
    def connect(self):
        """建立数据库连接"""
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(
                host=self.config["host"],
                port=self.config["port"],
                database=self.config["database"],
                user=self.config["user"],
                password=self.config["password"],
            )
        return self._conn
    
    def close(self):
        """关闭数据库连接"""
        if self._conn and not self._conn.closed:
            self._conn.close()
            self._conn = None
    
    def execute_query(self, query: str, params: Optional[Tuple] = None) -> pd.DataFrame:
        """执行查询并返回 DataFrame"""
        conn = self.connect()
        try:
            return pd.read_sql_query(query, conn, params=params)
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
        # 不转换大小写，保持数据库中的原始名称
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
        """获取指定 model 和 rule_code 下的所有字段"""
        schema = qident(model)
        query = f"""
        SELECT field_id, field, type
        FROM {schema}.field
        WHERE rule_code = %s AND type = 'numeric'
        ORDER BY field
        """
        df = self.execute_query(query, (rule_code,))
        return df.to_dict("records")
    
    def get_field_data(
        self,
        model: str,
        field_ids: List[int],
        time_range: Optional[Tuple[str, str]] = None,
    ) -> pd.DataFrame:
        """
        获取字段数据（包含 episode_id, field_id, value, sn, taskid, collected_at）
        
        Args:
            model: 模型名
            field_ids: 字段 ID 列表
            time_range: 时间范围 (start, end)，格式 YYYY-MM-DD
        
        Returns:
            DataFrame with columns: episode_id, field_id, value, sn, taskid, collected_at
        """
        schema = qident(model)
        
        # 构建 WHERE 条件
        where_clauses = [f"fv.field_id IN ({','.join(map(str, field_ids))})"]
        params = []
        
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
            e.collected_at
        FROM {schema}.field_value fv
        JOIN {schema}.episode e ON fv.episode_id = e.episode_id
        WHERE {where_str}
        ORDER BY e.collected_at, fv.field_id
        """
        
        return self.execute_query(query, tuple(params) if params else None)
    
    def get_thresholds(self, model: str, field: str) -> Optional[Dict[str, float]]:
        """获取字段的阈值（min, max）"""
        schema = qident(model)
        query = f"""
        SELECT min, max
        FROM {schema}.thresholds_base
        WHERE field = %s
        """
        df = self.execute_query(query, (field,))
        
        if df.empty:
            return None
        
        row = df.iloc[0]
        try:
            return {
                "min": float(row["min"]) if pd.notna(row["min"]) else None,
                "max": float(row["max"]) if pd.notna(row["max"]) else None,
            }
        except (ValueError, TypeError):
            return None
    
    def get_field_name(self, model: str, field_id: int) -> str:
        """根据 field_id 获取 field 名称"""
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
