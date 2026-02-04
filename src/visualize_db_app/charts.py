"""
图表生成模块
"""

from typing import List, Dict, Any, Tuple
import math
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dash import dcc, html
import dash_bootstrap_components as dbc


def _ensure_datetime_utc(df: pd.DataFrame, col: str = "collected_at") -> pd.DataFrame:
    if col in df.columns and not pd.api.types.is_datetime64_any_dtype(df[col]):
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    return df


def _apply_grouping(df: pd.DataFrame, group_by: str) -> Tuple[pd.DataFrame, str]:
    """
    返回 (df_with_group_col, group_key)
    group_key 是用于分组的列名
    """
    df = df.copy()
    df = _ensure_datetime_utc(df, "collected_at")

    if group_by == "sn":
        df["group_label"] = df.apply(lambda r: f"{r.get('area', 'N/A')}: {r.get('sn', 'N/A')}", axis=1)
        return df, "group_label"
    if group_by == "taskid":
        df["group_label"] = df["taskid"].astype(str)
        return df, "group_label"
    if group_by == "area":
        df["group_label"] = df["area"].astype(str)
        return df, "group_label"

    # time-based
    if group_by == "day":
        df["group_label"] = df["collected_at"].dt.strftime("%Y-%m-%d")
        return df, "group_label"
    if group_by == "week":
        df["group_label"] = df["collected_at"].dt.strftime("%Y-W%W")
        return df, "group_label"
    if group_by == "month":
        df["group_label"] = df["collected_at"].dt.strftime("%Y-%m")
        return df, "group_label"

    # fallback
    df["group_label"] = df.apply(lambda r: f"{r.get('area', 'N/A')}: {r.get('sn', 'N/A')}", axis=1)
    return df, "group_label"


def create_scatter_plot(
    df: pd.DataFrame,
    field_info_list: List[Dict[str, Any]],
    sort_by: str = "time",
    group_by: str = "sn",
) -> dcc.Graph:
    """（single 模式）保持你原来的实现逻辑：一个 Figure 叠 trace"""
    fig = go.Figure()

    for field_info in field_info_list:
        field_id = field_info["field_id"]
        field_name = field_info["field_name"]
        thresholds = field_info.get("thresholds")

        field_df = df[df["field_id"] == field_id].copy()
        if field_df.empty:
            continue

        field_df = _ensure_datetime_utc(field_df, "collected_at")

        sort_keys = []
        if sort_by == "sn":
            sort_keys.append("sn")
        elif sort_by == "taskid":
            sort_keys.append("taskid")
        elif sort_by == "area":
            sort_keys.append("area")
        else:
            sort_keys.append("collected_at")

        time_based_groups = ["day", "week", "month"]
        if group_by not in time_based_groups:
            if group_by == "sn" and "sn" not in sort_keys:
                sort_keys.append("sn")
            elif group_by == "taskid" and "taskid" not in sort_keys:
                sort_keys.append("taskid")
            elif group_by == "area" and "area" not in sort_keys:
                sort_keys.append("area")

        if "collected_at" not in sort_keys:
            sort_keys.append("collected_at")

        field_df = field_df.sort_values(sort_keys)

        field_df, group_key = _apply_grouping(field_df, group_by)
        unique_groups = field_df[group_key].unique()

        field_df["episode_index"] = range(1, len(field_df) + 1)

        for group_value in unique_groups:
            group_df = field_df[field_df[group_key] == group_value]

            hover_texts = []
            episode_ids = []
            for _, row in group_df.iterrows():
                area_sn = f"{row.get('area', 'N/A')}: {row.get('sn', 'N/A')}"
                hover_texts.append(
                    f"Field: {field_name}<br>"
                    f"Episode: {row['episode_id']}<br>"
                    f"SN: {area_sn}<br>"
                    f"TaskID: {row.get('taskid', 'N/A')}<br>"
                    f"Time: {row['collected_at']}<br>"
                    f"Value: {row['value']:.4f}"
                )
                episode_ids.append([row["episode_id"], field_id])

            fig.add_trace(
                go.Scatter(
                    x=group_df["episode_index"],
                    y=group_df["value"],
                    mode="markers",
                    name=str(group_value),
                    text=hover_texts,
                    customdata=episode_ids,
                    hovertemplate="%{text}<extra></extra>",
                    marker=dict(size=6),
                )
            )

        # 阈值线（保留原逻辑）
        if thresholds:
            x_range = [field_df["episode_index"].min(), field_df["episode_index"].max()]

            if thresholds.get("base"):
                base_thresholds = thresholds["base"]
                if base_thresholds and base_thresholds.get("min") is not None:
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
                if base_thresholds and base_thresholds.get("max") is not None:
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

            if thresholds.get("full"):
                full_thresholds = thresholds["full"]
                if full_thresholds and full_thresholds.get("min") is not None:
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
                if full_thresholds and full_thresholds.get("max") is not None:
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

    fig.update_layout(
        title="Field Value Trends",
        xaxis_title="Episode Index",
        yaxis_title="Value",
        hovermode="closest",
        legend=dict(orientation="v", yanchor="top", y=1, xanchor="left", x=1.02),
        height=600,
    )

    return dcc.Graph(id="field-chart", figure=fig, config={"displayModeBar": True, "displaylogo": False})


def create_scatter_plot_multi_subplots(
    df: pd.DataFrame,
    field_info_list: List[Dict[str, Any]],
    cols: int = 3,
    sort_by: str = "time",
    group_by: str = "sn",
) -> dcc.Graph:
    """
    multi 模式：一个 Figure + 子图网格（每页最多 9 个字段）
    - 共用 legend：legendgroup + 仅第一个子图 showlegend=True
    - x 轴使用全局 episode_index（由回调提前计算并写入 df）
    """
    if not field_info_list:
        fig = go.Figure()
        fig.update_layout(title="No fields")
        return dcc.Graph(id="field-chart", figure=fig, config={"displayModeBar": True, "displaylogo": False})

    df = df.copy()
    df = _ensure_datetime_utc(df, "collected_at")

    # group_label 全局生成（同一 episode 属性一致，因此全局一致）
    df, group_key = _apply_grouping(df, group_by)

    # legend 顺序：按全局 episode_index 的出现顺序
    if "episode_index" in df.columns:
        df_sorted_for_groups = df.sort_values("episode_index")
    else:
        df_sorted_for_groups = df.sort_values("collected_at")
    group_values_ordered = list(pd.unique(df_sorted_for_groups[group_key]))

    n = len(field_info_list)
    rows = math.ceil(n / cols)

    subplot_titles = [f["field_name"] for f in field_info_list]
    fig = make_subplots(
        rows=rows,
        cols=cols,
        subplot_titles=subplot_titles,
        horizontal_spacing=0.05,
        vertical_spacing=0.10,
    )

    # 只在第一个子图显示数据点的 legend
    legend_shown = False
    # 跟踪阈值线是否已在 legend 中显示（全局共用 4 条阈值线）
    threshold_legends = {
        "base_min": False,
        "base_max": False,
        "full_min": False,
        "full_max": False,
    }

    for i, field_info in enumerate(field_info_list):
        r = i // cols + 1
        c = i % cols + 1

        field_id = field_info["field_id"]
        field_name = field_info["field_name"]
        thresholds = field_info.get("thresholds")

        field_df = df[df["field_id"] == field_id].copy()
        if field_df.empty:
            continue

        # 每个字段内部只保留数值点
        field_df["value"] = pd.to_numeric(field_df["value"], errors="coerce")
        field_df = field_df.dropna(subset=["value"])
        if field_df.empty:
            continue

        # 以 episode_index 作为 x（统一全局排序）
        x_col = "episode_index" if "episode_index" in field_df.columns else "collected_at"

        for gv in group_values_ordered:
            group_df = field_df[field_df[group_key] == gv]
            if group_df.empty:
                continue

            hover_texts = []
            episode_ids = []
            for _, row in group_df.iterrows():
                area_sn = f"{row.get('area', 'N/A')}: {row.get('sn', 'N/A')}"
                hover_texts.append(
                    f"Field: {field_name}<br>"
                    f"Episode: {row['episode_id']}<br>"
                    f"SN: {area_sn}<br>"
                    f"TaskID: {row.get('taskid', 'N/A')}<br>"
                    f"Time: {row['collected_at']}<br>"
                    f"Value: {row['value']:.4f}"
                )
                episode_ids.append([row["episode_id"], field_id])

            showlegend = (not legend_shown)
            fig.add_trace(
                go.Scatter(
                    x=group_df[x_col],
                    y=group_df["value"],
                    mode="markers",
                    name=str(gv),
                    legendgroup=str(gv),
                    showlegend=showlegend,
                    text=hover_texts,
                    customdata=episode_ids,
                    hovertemplate="%{text}<extra></extra>",
                    marker=dict(size=5),
                ),
                row=r,
                col=c,
            )
        legend_shown = True

        # 阈值线：全局共用 legend（每种类型只在第一次出现时显示）
        if thresholds and "episode_index" in field_df.columns:
            x_min = field_df["episode_index"].min()
            x_max = field_df["episode_index"].max()
            x_range = [x_min, x_max]

            if thresholds.get("base"):
                base_t = thresholds["base"]
                if base_t and base_t.get("min") is not None:
                    show_in_legend = not threshold_legends["base_min"]
                    threshold_legends["base_min"] = True
                    fig.add_trace(
                        go.Scatter(
                            x=x_range,
                            y=[base_t["min"], base_t["min"]],
                            mode="lines",
                            name="Base Min",
                            legendgroup="base_min",
                            line=dict(color="red", dash="dash", width=1),
                            showlegend=show_in_legend,
                        ),
                        row=r,
                        col=c,
                    )
                if base_t and base_t.get("max") is not None:
                    show_in_legend = not threshold_legends["base_max"]
                    threshold_legends["base_max"] = True
                    fig.add_trace(
                        go.Scatter(
                            x=x_range,
                            y=[base_t["max"], base_t["max"]],
                            mode="lines",
                            name="Base Max",
                            legendgroup="base_max",
                            line=dict(color="red", dash="dot", width=1),
                            showlegend=show_in_legend,
                        ),
                        row=r,
                        col=c,
                    )

            if thresholds.get("full"):
                full_t = thresholds["full"]
                if full_t and full_t.get("min") is not None:
                    show_in_legend = not threshold_legends["full_min"]
                    threshold_legends["full_min"] = True
                    fig.add_trace(
                        go.Scatter(
                            x=x_range,
                            y=[full_t["min"], full_t["min"]],
                            mode="lines",
                            name="Full Min",
                            legendgroup="full_min",
                            line=dict(color="orange", dash="dash", width=1),
                            showlegend=show_in_legend,
                        ),
                        row=r,
                        col=c,
                    )
                if full_t and full_t.get("max") is not None:
                    show_in_legend = not threshold_legends["full_max"]
                    threshold_legends["full_max"] = True
                    fig.add_trace(
                        go.Scatter(
                            x=x_range,
                            y=[full_t["max"], full_t["max"]],
                            mode="lines",
                            name="Full Max",
                            legendgroup="full_max",
                            line=dict(color="orange", dash="dot", width=1),
                            showlegend=show_in_legend,
                        ),
                        row=r,
                        col=c,
                    )

    # 高度按行数自适应
    height = 260 * rows + 160

    fig.update_layout(
        title="Multi Field Trends (Subplots)",
        hovermode="closest",
        legend=dict(orientation="v", yanchor="top", y=1, xanchor="left", x=1.02),
        height=height,
        margin=dict(l=40, r=240, t=60, b=40),
    )

    return dcc.Graph(id="field-chart", figure=fig, config={"displayModeBar": True, "displaylogo": False})


def create_statistics_cards(df: pd.DataFrame, field_info_list: List[Dict[str, Any]], group_by: str = "sn") -> html.Div:
    """生成统计面板，支持动态分组"""
    cards = []
    
    # 根据 group_by 确定第一列标题和分组键
    if group_by == "taskid":
        group_label = "TaskID"
        group_key_func = lambda row: str(row.get("taskid", "N/A"))
    elif group_by == "area":
        group_label = "Area"
        group_key_func = lambda row: str(row.get("area", "N/A"))
    elif group_by in ["day", "week", "month"]:
        group_label = "Time Period"
        if group_by == "day":
            group_key_func = lambda row: pd.to_datetime(row.get("collected_at"), utc=True, errors="coerce").strftime("%Y-%m-%d") if pd.notna(row.get("collected_at")) else "N/A"
        elif group_by == "week":
            group_key_func = lambda row: pd.to_datetime(row.get("collected_at"), utc=True, errors="coerce").strftime("%Y-W%W") if pd.notna(row.get("collected_at")) else "N/A"
        else:  # month
            group_key_func = lambda row: pd.to_datetime(row.get("collected_at"), utc=True, errors="coerce").strftime("%Y-%m") if pd.notna(row.get("collected_at")) else "N/A"
    else:  # sn (default)
        group_label = "SN"
        group_key_func = lambda row: f"{row.get('area', 'N/A')}: {row.get('sn', 'N/A')}"

    for field_info in field_info_list:
        field_id = field_info["field_id"]
        field_name = field_info["field_name"]
        thresholds = field_info.get("thresholds")
        field_type = field_info.get("type", "numeric")

        field_df_all = df[df["field_id"] == field_id].copy()
        if field_df_all.empty:
            continue

        # 添加分组列
        field_df_all["group_key"] = field_df_all.apply(group_key_func, axis=1)

        stats_rows = []
        for group_value in field_df_all["group_key"].unique():
            group_df_all = field_df_all[field_df_all["group_key"] == group_value]
            total_count = len(group_df_all)
            
            # metadata (non_numeric) 字段特殊处理
            if field_type == "non_numeric":
                # 对于 metadata：value 已在 callbacks 中转换为 0.0（通过）或 1.0（不通过）
                # Missing: value == 1.0 表示原始值为空/N/A
                missing_count = (group_df_all["value"] == 1.0).sum()
                missing_rate = (missing_count / total_count * 100) if total_count > 0 else 0
                
                # 有效数据：value == 0.0（有值的记录）
                valid_count = (group_df_all["value"] == 0.0).sum()
                
                # Pass: value == 0.0（有值即为通过）
                pass_count = valid_count
                pass_rate = (pass_count / total_count * 100) if total_count > 0 else 0
                
                # Mean 对 metadata 不适用
                mean_val = float('nan')
            else:
                # numeric 字段的原有逻辑
                # 计算缺失值：空字符串、"N/A"、或转换为 numeric 后的 NaN
                group_df_all["value_numeric"] = pd.to_numeric(group_df_all["value"], errors="coerce")
                missing_mask = (
                    (group_df_all["value"] == "") | 
                    (group_df_all["value"] == "N/A") | 
                    (group_df_all["value_numeric"].isna())
                )
                missing_count = missing_mask.sum()
                missing_rate = (missing_count / total_count * 100) if total_count > 0 else 0
                
                # 过滤出有效数值
                group_df = group_df_all[~missing_mask].copy()
                valid_count = len(group_df)
                
                if valid_count > 0:
                    mean_val = group_df["value_numeric"].mean()
                else:
                    mean_val = float('nan')

                pass_count = 0
                pass_rate = None
                if thresholds and thresholds.get("base") and valid_count > 0:
                    base_thresholds = thresholds["base"]
                    if base_thresholds and base_thresholds.get("min") is not None and base_thresholds.get("max") is not None:
                        passed = group_df[
                            (group_df["value_numeric"] >= base_thresholds["min"]) & 
                            (group_df["value_numeric"] <= base_thresholds["max"])
                        ]
                        pass_count = len(passed)
                        pass_rate = pass_count / valid_count * 100 if valid_count > 0 else 0

            stats_rows.append(
                html.Tr(
                    [
                        html.Td(group_value),
                        html.Td(f"{mean_val:.4f}" if not pd.isna(mean_val) else "N/A"),
                        html.Td(f"{pass_rate:.2f}%" if pass_rate is not None else "N/A"),
                        html.Td(str(pass_count)),
                        html.Td(f"{missing_rate:.1f}%"),
                    ]
                )
            )

        if not stats_rows:
            continue

        table = dbc.Table(
            [
                html.Thead(
                    html.Tr(
                        [
                            html.Th(group_label),
                            html.Th("Mean"),
                            html.Th("Pass Rate"),
                            html.Th("Pass Count"),
                            html.Th("Missing Rate"),
                        ]
                    )
                ),
                html.Tbody(stats_rows),
            ],
            bordered=True,
            hover=True,
            responsive=True,
            striped=True,
            size="sm",
        )

        card = dbc.Card(
            [dbc.CardHeader(html.H6(field_name, className="mb-0")), dbc.CardBody(table)],
            className="mb-3",
        )
        cards.append(card)

    if not cards:
        return html.Div("暂无统计数据", className="text-muted")

    return html.Div([html.H5("Statistics", className="mb-3"), *cards])
