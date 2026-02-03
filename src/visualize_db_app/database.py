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


class DatabaseManager:
    """数据库管理器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._engine: Optional[Engine] = None
    
    def connect(self) -> Engine:
        """建立数据库连接"""
        if self._engine is None:
            # 构建 SQLAlchemy 连接字符串
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
        """执行查询并返回 DataFrame"""
        engine = self.connect()
        try:
            # SQLAlchemy 2.0+ 需要使用 text() 包装 SQL 语句
            if params:
                # 将 psycopg2 风格的 %s 参数转换为 SQLAlchemy 的命名参数
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
            e.area,
            e.collected_at
        FROM {schema}.field_value fv
        JOIN {schema}.episode e ON fv.episode_id = e.episode_id
        WHERE {where_str}
        ORDER BY e.collected_at, fv.field_id
        """
        
        return self.execute_query(query, tuple(params) if params else None)
    
    def get_thresholds(self, model: str, field: str) -> Optional[Dict[str, Dict[str, float]]]:
        """获取字段的阈值（base 和 full 的 min, max）"""
        schema = qident(model)
        
        result = {}
        
        # 获取 base 阈值
        query_base = f"""
        SELECT min, max
        FROM {schema}.thresholds_base
        WHERE field = %s
        """
        df_base = self.execute_query(query_base, (field,))
        
        if not df_base.empty:
            row = df_base.iloc[0]
            try:
                result["base"] = {
                    "min": float(row["min"]) if pd.notna(row["min"]) else None,
                    "max": float(row["max"]) if pd.notna(row["max"]) else None,
                }
            except (ValueError, TypeError):
                result["base"] = None
        else:
            result["base"] = None
        
        # 获取 full 阈值
        query_full = f"""
        SELECT min, max
        FROM {schema}.thresholds_full
        WHERE field = %s
        """
        df_full = self.execute_query(query_full, (field,))
        
        if not df_full.empty:
            row = df_full.iloc[0]
            try:
                result["full"] = {
                    "min": float(row["min"]) if pd.notna(row["min"]) else None,
                    "max": float(row["max"]) if pd.notna(row["max"]) else None,
                }
            except (ValueError, TypeError):
                result["full"] = None
        else:
            result["full"] = None
        
        # 如果两组阈值都为空，返回 None
        if result["base"] is None and result["full"] is None:
            return None
        
        return result
    
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
