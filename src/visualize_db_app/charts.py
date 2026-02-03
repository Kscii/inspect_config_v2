# src/visualize_db_app/charts.py
"""
图表生成模块
"""

from typing import List, Dict, Any
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dash import dcc, html
import dash_bootstrap_components as dbc


def create_scatter_plot(df: pd.DataFrame, field_info_list: List[Dict[str, Any]], sort_by: str = "time", group_by: str = "sn") -> dcc.Graph:
    """
    创建散点图
    
    Args:
        df: 包含 episode_id, field_id, value, sn, taskid, collected_at 的 DataFrame
        field_info_list: 字段信息列表，每项包含 field_id, field_name, thresholds
        sort_by: 排序方式，"time"、"sn" 或 "taskid"
        group_by: 分类方式，"sn"、"taskid"、"day"、"week" 或 "month"
    
    Returns:
        dcc.Graph 组件
    """
    # 创建图表
    fig = go.Figure()
    
    # 为每个字段添加轨迹
    for field_info in field_info_list:
        field_id = field_info["field_id"]
        field_name = field_info["field_name"]
        thresholds = field_info.get("thresholds")
        
        # 过滤当前字段的数据
        field_df = df[df["field_id"] == field_id].copy()
        
        if field_df.empty:
            continue
        
        # 确保 collected_at 是 datetime 类型
        if not pd.api.types.is_datetime64_any_dtype(field_df["collected_at"]):
            field_df["collected_at"] = pd.to_datetime(field_df["collected_at"], utc=True)
        
        # 构建排序键列表
        sort_keys = []
        
        # 1. 添加用户选择的排序方式
        if sort_by == "sn":
            sort_keys.append("sn")
        elif sort_by == "taskid":
            sort_keys.append("taskid")
        elif sort_by == "area":
            sort_keys.append("area")
        elif sort_by == "time":
            sort_keys.append("collected_at")
        
        # 2. 如果分类方式不是按时间分类，添加分类字段
        time_based_groups = ["day", "week", "month"]
        if group_by not in time_based_groups:
            # 添加分类字段（如果不在排序键中）
            if group_by == "sn" and "sn" not in sort_keys:
                sort_keys.append("sn")
            elif group_by == "taskid" and "taskid" not in sort_keys:
                sort_keys.append("taskid")
            elif group_by == "area" and "area" not in sort_keys:
                sort_keys.append("area")
        
        # 3. 最后添加时间（如果不在排序键中）
        if "collected_at" not in sort_keys:
            sort_keys.append("collected_at")
        
        # 执行排序
        field_df = field_df.sort_values(sort_keys)
        
        # 根据分类方式创建分组
        if group_by == "sn":
            group_key = "sn"
            unique_groups = field_df["sn"].unique()
        elif group_by == "taskid":
            group_key = "taskid"
            unique_groups = field_df["taskid"].unique()
        elif group_by == "area":
            group_key = "area"
            unique_groups = field_df["area"].unique()
        elif group_by == "day":
            # 按日分类
            # 再次确保 collected_at 是 datetime 类型（防止 sort_values 后类型降级）
            if not pd.api.types.is_datetime64_any_dtype(field_df["collected_at"]):
                field_df["collected_at"] = pd.to_datetime(field_df["collected_at"], utc=True)
            field_df["group_label"] = field_df["collected_at"].dt.strftime("%Y-%m-%d")
            group_key = "group_label"
            unique_groups = field_df["group_label"].unique()
        elif group_by == "week":
            # 按周分类（ISO周格式：YYYY-Www）
            # 再次确保 collected_at 是 datetime 类型（防止 sort_values 后类型降级）
            if not pd.api.types.is_datetime64_any_dtype(field_df["collected_at"]):
                field_df["collected_at"] = pd.to_datetime(field_df["collected_at"], utc=True)
            field_df["group_label"] = field_df["collected_at"].dt.strftime("%Y-W%W")
            group_key = "group_label"
            unique_groups = field_df["group_label"].unique()
        elif group_by == "month":
            # 按月分类
            # 再次确保 collected_at 是 datetime 类型（防止 sort_values 后类型降级）
            if not pd.api.types.is_datetime64_any_dtype(field_df["collected_at"]):
                field_df["collected_at"] = pd.to_datetime(field_df["collected_at"], utc=True)
            field_df["group_label"] = field_df["collected_at"].dt.strftime("%Y-%m")
            group_key = "group_label"
            unique_groups = field_df["group_label"].unique()
        else:
            # 默认按 SN 分类
            group_key = "sn"
            unique_groups = field_df["sn"].unique()
        
        # 添加 episode 序号（1-based）
        field_df["episode_index"] = range(1, len(field_df) + 1)
        
        # 为每个分组添加一个轨迹
        for group_value in unique_groups:
            group_df = field_df[field_df[group_key] == group_value]
            
            # 构建 hover 文本
            hover_texts = []
            episode_ids = []
            for _, row in group_df.iterrows():
                hover_texts.append(
                    f"Episode: {row['episode_id']}<br>"
                    f"SN: {row['sn']}<br>"
                    f"TaskID: {row.get('taskid', 'N/A')}<br>"
                    f"Area: {row.get('area', 'N/A')}<br>"
                    f"Time: {row['collected_at']}<br>"
                    f"Value: {row['value']:.4f}"
                )
                episode_ids.append(row['episode_id'])
            
            # 设置图例名称
            legend_name = str(group_value)
            
            fig.add_trace(
                go.Scatter(
                    x=group_df["episode_index"],
                    y=group_df["value"],
                    mode="markers",
                    name=legend_name,
                    text=hover_texts,
                    customdata=episode_ids,  # 存储episode_id以便点击时获取
                    hovertemplate="%{text}<extra></extra>",
                    marker=dict(size=6),
                )
            )
        
        # 添加阈值线
        if thresholds:
            x_range = [field_df["episode_index"].min(), field_df["episode_index"].max()]
            
            # Base 阈值线
            if thresholds.get("base"):
                base_thresholds = thresholds["base"]
                if base_thresholds.get("min") is not None:
                    fig.add_trace(
                        go.Scatter(
                            x=x_range,
                            y=[base_thresholds["min"], base_thresholds["min"]],
                            mode="lines",
                            name="Base Min",
                            line=dict(color="red", dash="dash", width=2),
                            showlegend=True,
                        )
                    )
                
                if base_thresholds.get("max") is not None:
                    fig.add_trace(
                        go.Scatter(
                            x=x_range,
                            y=[base_thresholds["max"], base_thresholds["max"]],
                            mode="lines",
                            name="Base Max",
                            line=dict(color="red", dash="dot", width=2),
                            showlegend=True,
                        )
                    )
            
            # Full 阈值线
            if thresholds.get("full"):
                full_thresholds = thresholds["full"]
                if full_thresholds.get("min") is not None:
                    fig.add_trace(
                        go.Scatter(
                            x=x_range,
                            y=[full_thresholds["min"], full_thresholds["min"]],
                            mode="lines",
                            name="Full Min",
                            line=dict(color="orange", dash="dash", width=2),
                            showlegend=True,
                        )
                    )
                
                if full_thresholds.get("max") is not None:
                    fig.add_trace(
                        go.Scatter(
                            x=x_range,
                            y=[full_thresholds["max"], full_thresholds["max"]],
                            mode="lines",
                            name="Full Max",
                            line=dict(color="orange", dash="dot", width=2),
                            showlegend=True,
                        )
                    )
    
    # 更新布局
    fig.update_layout(
        title="Field Value Trends",
        xaxis_title="Episode Index",
        yaxis_title="Value",
        hovermode="closest",
        legend=dict(
            orientation="v",
            yanchor="top",
            y=1,
            xanchor="left",
            x=1.02,
        ),
        height=600,
    )
    
    return dcc.Graph(id="field-chart", figure=fig, config={"displayModeBar": True, "displaylogo": False})


def create_statistics_cards(df: pd.DataFrame, field_info_list: List[Dict[str, Any]]) -> html.Div:
    """
    创建统计面板（分 SN 显示）
    
    Args:
        df: 包含 episode_id, field_id, value, sn, collected_at 的 DataFrame
        field_info_list: 字段信息列表
    
    Returns:
        html.Div 包含统计卡片
    """
    cards = []
    
    for field_info in field_info_list:
        field_id = field_info["field_id"]
        field_name = field_info["field_name"]
        thresholds = field_info.get("thresholds")
        
        # 过滤当前字段的数据
        field_df = df[df["field_id"] == field_id].copy()
        
        if field_df.empty:
            continue
        
        # 按 SN 分组统计
        sn_stats = []
        for sn in field_df["sn"].unique():
            sn_df = field_df[field_df["sn"] == sn]
            
            # 计算统计指标
            mean_val = sn_df["value"].mean()
            std_val = sn_df["value"].std()
            count = len(sn_df)
            
            # 计算通过率（如果有阈值，使用 base 阈值）
            pass_rate = None
            if thresholds and thresholds.get("base"):
                base_thresholds = thresholds["base"]
                if base_thresholds.get("min") is not None and base_thresholds.get("max") is not None:
                    passed = sn_df[
                        (sn_df["value"] >= base_thresholds["min"]) & (sn_df["value"] <= base_thresholds["max"])
                    ]
                pass_rate = len(passed) / count * 100 if count > 0 else 0
            
            sn_stats.append(
                html.Tr(
                    [
                        html.Td(sn),
                        html.Td(f"{mean_val:.4f}"),
                        html.Td(f"{std_val:.4f}"),
                        html.Td(f"{pass_rate:.2f}%" if pass_rate is not None else "N/A"),
                        html.Td(str(count)),
                    ]
                )
            )
        
        # 创建统计表格
        table = dbc.Table(
            [
                html.Thead(
                    html.Tr(
                        [
                            html.Th("SN"),
                            html.Th("Mean"),
                            html.Th("Std Dev"),
                            html.Th("Pass Rate"),
                            html.Th("Count"),
                        ]
                    )
                ),
                html.Tbody(sn_stats),
            ],
            bordered=True,
            hover=True,
            responsive=True,
            striped=True,
            size="sm",
        )
        
        # 创建卡片
        card = dbc.Card(
            [
                dbc.CardHeader(html.H6(field_name, className="mb-0")),
                dbc.CardBody(table),
            ],
            className="mb-3",
        )
        
        cards.append(card)
    
    if not cards:
        return html.Div("暂无统计数据", className="text-muted")
    
    return html.Div(
        [
            html.H5("Statistics by SN", className="mb-3"),
            *cards,
        ]
    )
