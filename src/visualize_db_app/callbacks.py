# src/visualize_db_app/callbacks.py
"""
Dash 回调函数
"""

from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
import math

import pandas as pd
from dash import Input, Output, State, callback, html, ctx, no_update, ALL
import dash_bootstrap_components as dbc

from .app import app
from .database import DatabaseManager, qident
from .charts import create_scatter_plot, create_scatter_plot_multi_subplots, create_statistics_cards


MULTI_PAGE_SIZE = 9   # 每页 9 张（3x3）


def get_db_manager() -> DatabaseManager:
    """获取数据库管理器实例"""
    db_config = app.server.config.get("DB_CONFIG")
    if not db_config:
        raise ValueError("Database configuration not found")
    return DatabaseManager(db_config)


def _calc_time_range(value: str) -> Optional[Tuple[str, str]]:
    if value == "all":
        return None
    end = datetime.now()
    if value == "7d":
        start = end - timedelta(days=7)
    elif value == "30d":
        start = end - timedelta(days=30)
    elif value == "90d":
        start = end - timedelta(days=90)
    else:
        return None
    return (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))


def _compute_global_episode_index(df: pd.DataFrame, sort_by: str, group_by: str) -> pd.DataFrame:
    """
    multi 模式：基于 episode 维度先算全局顺序，再回填到 df 中。
    统一 sort_by / group_by 生效。
    """
    if df.empty:
        return df

    # episode 维 unique
    epi = df[["episode_id", "sn", "taskid", "area", "collected_at"]].drop_duplicates().copy()
    if not pd.api.types.is_datetime64_any_dtype(epi["collected_at"]):
        epi["collected_at"] = pd.to_datetime(epi["collected_at"], utc=True, errors="coerce")

    # 排序键（统一）
    sort_keys: List[str] = []
    if sort_by == "sn":
        sort_keys.append("sn")
    elif sort_by == "taskid":
        sort_keys.append("taskid")
    elif sort_by == "area":
        sort_keys.append("area")
    else:
        sort_keys.append("collected_at")

    # 如果 group_by 非时间分组，也把分组字段纳入排序键（保证组内稳定）
    time_groups = {"day", "week", "month"}
    if group_by not in time_groups:
        if group_by == "sn" and "sn" not in sort_keys:
            sort_keys.append("sn")
        elif group_by == "taskid" and "taskid" not in sort_keys:
            sort_keys.append("taskid")
        elif group_by == "area" and "area" not in sort_keys:
            sort_keys.append("area")

    if "collected_at" not in sort_keys:
        sort_keys.append("collected_at")

    epi = epi.sort_values(sort_keys, kind="mergesort")  # 稳定排序
    epi["episode_index"] = range(1, len(epi) + 1)

    # 回填
    df = df.merge(epi[["episode_id", "episode_index"]], on="episode_id", how="left")
    return df


# -------------------------
# 视图模式：radio -> store，并清理选择和折叠状态
# -------------------------
@callback(
    Output("view-mode-store", "data"),
    Output("selected-fields-store", "data", allow_duplicate=True),
    Output("selected-rule-store", "data", allow_duplicate=True),
    Output("multi-page-store", "data", allow_duplicate=True),
    Output("current-model-store", "data", allow_duplicate=True),
    Output("expanded-model-store", "data", allow_duplicate=True),
    Output("expanded-rule-store", "data", allow_duplicate=True),
    Output("selected-episode-store", "data", allow_duplicate=True),
    Input("view-mode-radio", "value"),
    prevent_initial_call=True,
)
def set_view_mode(mode: str):
    # 切模式时清空选择和折叠状态，避免 single/multi 状态互相污染
    if mode not in ("single", "multi"):
        mode = "single"
    return mode, [], None, {"page": 1}, None, None, None, None


# -------------------------
# 导航树渲染：响应视图模式和折叠状态变化
# -------------------------
@callback(
    Output("navigation-tree", "children"),
    Input("navigation-tree", "id"),
    Input("view-mode-store", "data"),
    Input("expanded-model-store", "data"),
    State("expanded-rule-store", "data"),
)
def render_navigation_tree(_, view_mode, expanded_model, expanded_rule):
    try:
        db = get_db_manager()
        models = db.get_models()
        if not models:
            return html.Div("未找到任何模型", className="text-muted")

        tree_items = []
        for model in models:
            model_display = model
            rule_codes = db.get_rule_codes(model)

            rule_items = []
            for rule_code in rule_codes:
                # rule 项
                rule_items.append(
                    html.Div(
                        [
                            dbc.ListGroupItem(
                                f"📋 {rule_code}",
                                id={"type": "rule-item", "model": model, "rule": rule_code},
                                action=True,
                                className="ps-4",
                                style={"fontWeight": "500"},
                            ),
                            # 保留 field-list 容器：
                            # - single: 会填充真实 field-item
                            # - multi: 保持空且隐藏（无法展开到字段）
                            html.Div(
                                [],
                                id={"type": "field-list", "model": model, "rule": rule_code},
                                style={"display": "none"},
                            ),
                        ]
                    )
                )

            # 根据 expanded_model 决定 rule-list 是否显示
            rule_list_style = {"display": "block"} if expanded_model == model else {"display": "none"}

            tree_items.append(
                html.Div(
                    [
                        dbc.ListGroupItem(
                            f"{model_display}",
                            id={"type": "model-item", "model": model},
                            action=True,
                            color="primary",
                            className="fw-bold",
                        ),
                        html.Div(rule_items, id={"type": "rule-list", "model": model}, style=rule_list_style),
                    ],
                    className="mb-2",
                )
            )

        return dbc.ListGroup(tree_items, flush=True)

    except Exception as e:
        return html.Div(f"加载导航树失败: {str(e)}", className="text-danger")


@callback(
    Output("expanded-model-store", "data"),
    Input({"type": "model-item", "model": ALL}, "n_clicks"),
    State("expanded-model-store", "data"),
    State({"type": "model-item", "model": ALL}, "id"),
    prevent_initial_call=True,
)
def toggle_model(n_clicks_list, expanded_model, model_ids):
    if not ctx.triggered:
        return no_update

    triggered_id = ctx.triggered_id
    if not triggered_id or "model" not in triggered_id:
        return no_update

    triggered_model = triggered_id["model"]
    new_expanded_model = None if expanded_model == triggered_model else triggered_model

    return new_expanded_model


# -------------------------
# single 模式：点击 rule 展开/折叠字段列表（但这里我们懒加载字段：点击 rule 时才拉字段并填充 field-list）
# 注意：为了最小改动，我们单独写一个回调来填充 field-list children
# -------------------------
@callback(
    Output({"type": "field-list", "model": ALL, "rule": ALL}, "children"),
    Output({"type": "field-list", "model": ALL, "rule": ALL}, "style"),
    Output("expanded-rule-store", "data"),
    Input({"type": "rule-item", "model": ALL, "rule": ALL}, "n_clicks"),
    State({"type": "rule-item", "model": ALL, "rule": ALL}, "id"),
    State("expanded-rule-store", "data"),
    State("view-mode-store", "data"),
    prevent_initial_call=True,
)
def toggle_rule_and_render_fields(n_clicks_list, rule_ids, expanded_rule, view_mode):
    # helper：为 ALL 输出构造 no_update 列表
    n = len(rule_ids) if rule_ids else 0
    def _no_updates():
        return [no_update] * n, [no_update] * n, no_update

    # multi 模式：不允许展开字段
    if view_mode != "single":
        return _no_updates()

    if not ctx.triggered:
        return _no_updates()

    triggered_id = ctx.triggered_id
    if not triggered_id or "rule" not in triggered_id:
        return _no_updates()

    triggered_model = triggered_id["model"]
    triggered_rule = triggered_id["rule"]
    triggered_info = {"model": triggered_model, "rule": triggered_rule}

    new_expanded_rule = None if expanded_rule == triggered_info else triggered_info

    db = get_db_manager()

    new_children_list = []
    new_styles = []

    # 为每个 field-list 生成 children/style
    for rid in rule_ids:
        model = rid["model"]
        rule = rid["rule"]

        if new_expanded_rule and model == new_expanded_rule["model"] and rule == new_expanded_rule["rule"]:
            fields = db.get_fields(model, rule)
            field_items = [
                dbc.ListGroupItem(
                    f.get("field_name", f["field"]),
                    id={"type": "field-item", "model": model, "rule": rule, "field_id": f["field_id"]},
                    action=True,
                    className="ps-5",
                    style={"fontSize": "0.85rem"},
                )
                for f in fields
            ]
            new_children_list.append(field_items)
            new_styles.append({"display": "block"})
        else:
            new_children_list.append([])
            new_styles.append({"display": "none"})

    return new_children_list, new_styles, new_expanded_rule


# -------------------------
# single 模式：字段选择（保持你原逻辑）
# -------------------------
@callback(
    Output("selected-fields-store", "data", allow_duplicate=True),
    Output("current-model-store", "data", allow_duplicate=True),
    Input({"type": "field-item", "model": ALL, "rule": ALL, "field_id": ALL}, "n_clicks"),
    State("selected-fields-store", "data"),
    State("current-model-store", "data"),
    State("view-mode-store", "data"),
    prevent_initial_call=True,
)
def select_field(n_clicks_list, selected_fields, current_model, view_mode):
    if view_mode != "single":
        return no_update, no_update

    if not ctx.triggered:
        return no_update, no_update

    triggered_id = ctx.triggered_id
    if not triggered_id:
        return no_update, no_update

    model = triggered_id["model"]
    field_id = triggered_id["field_id"]

    if selected_fields is None:
        selected_fields = []

    if current_model and current_model != model:
        selected_fields = []

    field_info = {"model": model, "field_id": field_id}

    if selected_fields and selected_fields[0] == field_info:
        selected_fields = []
    else:
        selected_fields = [field_info]

    return selected_fields, model


# -------------------------
# multi 模式：点击 rule 选择 rule + 重置页码
# -------------------------
@callback(
    Output("selected-rule-store", "data", allow_duplicate=True),
    Output("multi-page-store", "data", allow_duplicate=True),
    Output("selected-fields-store", "data", allow_duplicate=True),
    Output("current-model-store", "data", allow_duplicate=True),
    Input({"type": "rule-item", "model": ALL, "rule": ALL}, "n_clicks"),
    State("view-mode-store", "data"),
    prevent_initial_call=True,
)
def select_rule_multi(n_clicks_list, view_mode):
    if view_mode != "multi":
        return no_update, no_update, no_update, no_update
    if not ctx.triggered:
        return no_update, no_update, no_update, no_update

    triggered_id = ctx.triggered_id
    if not triggered_id:
        return no_update, no_update, no_update, no_update

    model = triggered_id["model"]
    rule = triggered_id["rule"]

    # 先清空 selected_fields，后续由分页回调填充第一页的 9 个字段
    return {"model": model, "rule": rule}, {"page": 1}, [], model


# -------------------------
# multi 模式：分页按钮（上一页/下一页）
# -------------------------
@callback(
    Output("multi-page-store", "data", allow_duplicate=True),
    Input("multi-prev-btn", "n_clicks"),
    Input("multi-next-btn", "n_clicks"),
    State("multi-page-store", "data"),
    State("multi-total-pages-store", "data"),
    State("view-mode-store", "data"),
    prevent_initial_call=True,
)
def change_multi_page(prev_clicks, next_clicks, page_data, total_pages, view_mode):
    if view_mode != "multi":
        return no_update

    page = int((page_data or {}).get("page", 1))
    total = int(total_pages or 1)

    trig = ctx.triggered_id
    if trig == "multi-prev-btn":
        page = max(1, page - 1)
    elif trig == "multi-next-btn":
        page = min(total, page + 1)

    return {"page": page}


# -------------------------
# multi 模式：根据 selected_rule + page 计算本页 9 个字段，并更新分页 UI
# -------------------------
@callback(
    Output("selected-fields-store", "data", allow_duplicate=True),
    Output("multi-total-pages-store", "data"),
    Output("multi-pagination-container", "style"),
    Output("multi-page-indicator", "children"),
    Output("multi-prev-btn", "disabled"),
    Output("multi-next-btn", "disabled"),
    Input("selected-rule-store", "data"),
    Input("multi-page-store", "data"),
    State("view-mode-store", "data"),
    prevent_initial_call=True,
)
def update_multi_page_fields(selected_rule, page_data, view_mode):
    if view_mode != "multi":
        return [], 1, {"display": "none"}, "", True, True

    if not selected_rule:
        return [], 1, {"display": "none"}, "", True, True

    model = selected_rule["model"]
    rule = selected_rule["rule"]
    page = int((page_data or {}).get("page", 1))

    db = get_db_manager()
    fields = db.get_fields(model, rule)  # 已按 field 排序

    total_fields = len(fields)
    total_pages = max(1, math.ceil(total_fields / MULTI_PAGE_SIZE))
    page = max(1, min(page, total_pages))

    start = (page - 1) * MULTI_PAGE_SIZE
    end = min(total_fields, start + MULTI_PAGE_SIZE)
    page_fields = fields[start:end]

    selected_fields = [{"model": model, "field_id": int(f["field_id"])} for f in page_fields]

    indicator = f"第 {page}/{total_pages} 页（本页 {len(page_fields)} / 总字段 {total_fields}）"
    prev_disabled = (page <= 1)
    next_disabled = (page >= total_pages)

    return selected_fields, total_pages, {"display": "block"}, indicator, prev_disabled, next_disabled


# -------------------------
# 已选信息显示：single 显示字段；multi 显示 model+rule+本页字段
# -------------------------
@callback(
    Output("selected-info-display", "children"),
    Input("selected-fields-store", "data"),
    Input("selected-episode-store", "data"),
    Input("selected-rule-store", "data"),
    State("current-model-store", "data"),
    State("view-mode-store", "data"),
    State("multi-page-store", "data"),
    State("multi-total-pages-store", "data"),
)
def update_selected_info_display(
    selected_fields,
    episode_data,
    selected_rule,
    current_model,
    view_mode,
    page_data,
    total_pages,
):
    info_items = []

    db = get_db_manager()

    # --- 选择信息 ---
    if view_mode == "multi":
        if selected_rule:
            info_items.append(
                html.Div(
                    [
                        html.Strong("已选 Rule： "),
                        dbc.Badge(f"{selected_rule['model']} / {selected_rule['rule']}", color="primary", className="me-2"),
                        html.Span(f"页码：{(page_data or {}).get('page', 1)}/{total_pages}", className="text-muted small"),
                    ],
                    className="mb-2",
                )
            )
        else:
            info_items.append(
                html.Div(
                    [html.Strong("已选 Rule： "), html.Span("尚未选择 Rule Code", className="text-muted")],
                    className="mb-2",
                )
            )

    # --- Episode 信息 ---
    info_items.append(html.Hr())

    # 始终显示 Episode 和字段信息
    current_field_val = ""
    episode_id_val = ""
    sn_val = ""
    taskid_val = ""
    area_val = ""
    collected_at_val = ""
    filename_val = ""

    if episode_data and current_model:
        try:
            episode_id = episode_data.get("episode_id")
            field_id = episode_data.get("field_id")
            
            if episode_id:
                schema = qident(current_model)
                query = f"""
                SELECT episode_id, taskid, model, area, sn, collected_at, filename
                FROM {schema}.episode
                WHERE episode_id = %s
                """
                df = db.execute_query(query, (episode_id,))
                if not df.empty:
                    episode_info = df.iloc[0]
                    episode_id_val = str(episode_info["episode_id"])
                    sn_val = str(episode_info["sn"])
                    taskid_val = str(episode_info["taskid"])
                    area_val = str(episode_info.get("area", "N/A"))
                    collected_at_val = str(episode_info["collected_at"])
                    filename_val = str(episode_info["filename"])
            
            # 获取当前字段
            if field_id:
                schema = qident(current_model)
                field_query = f"""
                SELECT field
                FROM {schema}.field
                WHERE field_id = %s
                """
                field_df = db.execute_query(field_query, (field_id,))
                if not field_df.empty:
                    current_field_val = str(field_df.iloc[0]["field"])
        except Exception as e:
            info_items.append(html.Div([html.Span(f"错误: {str(e)}", className="text-danger small")]))

    # 始终显示这些字段（有值则显示，无值则为空）
    info_items.extend(
        [
            html.Div([html.Strong("当前字段: "), html.Span(current_field_val, style={"wordBreak": "break-all", "fontSize": "0.85rem"})], className="mb-1"),
            html.Div([html.Strong("Episode ID: "), html.Span(episode_id_val)], className="mb-1"),
            html.Div([html.Strong("SN: "), html.Span(sn_val)], className="mb-1"),
            html.Div([html.Strong("TaskID: "), html.Span(taskid_val)], className="mb-1"),
            html.Div([html.Strong("采集地点: "), html.Span(area_val)], className="mb-1"),
            html.Div([html.Strong("采集时间: "), html.Span(collected_at_val)], className="mb-1"),
            html.Div([html.Strong("文件名: "), html.Span(filename_val)], className="mb-1"),
        ]
    )

    return html.Div(info_items, className="p-2 bg-light rounded")


# -------------------------
# 图表更新：single 用旧图；multi 用子图（每页 9 个字段）
# -------------------------
@callback(
    Output("chart-container", "children"),
    Output("statistics-panel", "children"),
    Input("selected-fields-store", "data"),
    Input("time-range-dropdown", "value"),
    Input("sort-by-dropdown", "value"),
    Input("group-by-dropdown", "value"),
    State("view-mode-store", "data"),
    State("selected-rule-store", "data"),
)
def update_chart(selected_fields, time_range, sort_by, group_by, view_mode, selected_rule):
    if not selected_fields:
        if view_mode == "multi":
            return html.Div("请从左侧导航树选择 Rule Code", className="text-muted text-center mt-5"), None
        return html.Div("请从左侧导航树选择字段", className="text-muted text-center mt-5"), None

    try:
        db = get_db_manager()
        model = selected_fields[0]["model"]
        field_ids = [int(f["field_id"]) for f in selected_fields]

        # 时间范围
        time_range_tuple = _calc_time_range(time_range)

        # 拉数据
        df = db.get_field_data(model, field_ids, time_range_tuple)
        if df.empty:
            return html.Div("未找到符合条件的数据", className="text-muted text-center mt-5"), None

        # field 信息批量获取（包含 field、field_name 和 type）
        field_info_map = db.get_field_info_batch(model, field_ids)
        
        # 根据字段类型转换 value
        def convert_value(row):
            fid = row["field_id"]
            if fid not in field_info_map:
                return pd.NA
            
            field_type = field_info_map[fid].get("type", "numeric")
            value = row["value"]
            
            if field_type == "non_numeric":
                # non_numeric: 空或不存在 → 1, 存在且不为空 → 0
                if pd.isna(value) or value == "" or value == "N/A":
                    return 1.0
                else:
                    return 0.0
            else:
                # numeric: 正常转换为数值
                try:
                    return float(value)
                except (ValueError, TypeError):
                    return pd.NA
        
        df["value"] = df.apply(convert_value, axis=1)
        df = df.dropna(subset=["value"])
        if df.empty:
            return html.Div("未找到可用的数值数据（value 全部无法转为数值）", className="text-muted text-center mt-5"), None
        
        # 用 field（不是 field_name）来查询阈值
        fields_for_thresholds = [field_info_map[fid]["field"] for fid in field_ids if fid in field_info_map]
        thresholds_map = db.get_thresholds_batch(model, fields_for_thresholds)

        field_info_list = []
        for fid in field_ids:
            if fid not in field_info_map:
                continue
            info = field_info_map[fid]
            field_info_list.append(
                {
                    "field_id": fid,
                    "field_name": info["display_name"],  # 用于图表显示
                    "thresholds": thresholds_map.get(info["field"]),  # 用 field 查询阈值
                }
            )

        # multi：先算全局 episode 顺序（统一 sort/group）
        if view_mode == "multi":
            df = _compute_global_episode_index(df, sort_by=sort_by, group_by=group_by)
            chart = create_scatter_plot_multi_subplots(
                df,
                field_info_list,
                cols=3,
                sort_by=sort_by,
                group_by=group_by,
            )
        else:
            chart = create_scatter_plot(df, field_info_list, sort_by, group_by)

        stats = create_statistics_cards(df, field_info_list, group_by)
        return chart, stats

    except Exception as e:
        return html.Div(f"图表生成失败: {str(e)}", className="text-danger text-center mt-5"), None


@callback(
    Output("selected-episode-store", "data"),
    Input("field-chart", "clickData"),
    prevent_initial_call=True,
)
def capture_click_data(click_data):
    """捕获图表点击事件（multi 子图也适用，因为仍是同一个 field-chart）"""
    if not click_data or "points" not in click_data or not click_data["points"]:
        return no_update

    point = click_data["points"][0]
    if "customdata" in point:
        customdata = point["customdata"]
        if isinstance(customdata, list) and len(customdata) >= 2:
            return {"episode_id": customdata[0], "field_id": customdata[1]}
        else:
            # 兼容旧格式
            return {"episode_id": customdata}

    return no_update
