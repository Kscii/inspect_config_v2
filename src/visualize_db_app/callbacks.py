# src/visualize_db_app/callbacks.py
"""
Dash 回调函数
"""

from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
import math
import time
import json as _json

import pandas as pd
from dash import Input, Output, State, callback, html, ctx, no_update, ALL
import dash_bootstrap_components as dbc

from .app import app
from .database import DatabaseManager, qident
from .charts import create_scatter_plot, create_scatter_plot_multi_subplots, create_statistics_cards


# -------------------------
# Clientside callback: 监听键盘方向键
# -------------------------
app.clientside_callback(
    """
    function(n) {
        if (!window.keyboardListenerAdded) {
            document.addEventListener('keydown', function(event) {
                if (event.key === 'ArrowLeft') {
                    const prevBtn = document.getElementById('keyboard-prev-trigger');
                    if (prevBtn) prevBtn.click();
                } else if (event.key === 'ArrowRight') {
                    const nextBtn = document.getElementById('keyboard-next-trigger');
                    if (nextBtn) nextBtn.click();
                }
            });
            window.keyboardListenerAdded = true;
        }
        return window.dash_clientside.no_update;
    }
    """,
    Output("keyboard-listener-output", "children"),
    Input("view-mode-store", "data"),
)


MULTI_PAGE_SIZE = 9   # 每页 9 张（3x3）


def get_db_manager() -> DatabaseManager:
    """获取数据库管理器实例"""
    db_config = app.server.config.get("DB_CONFIG")
    if not db_config:
        raise ValueError("Database configuration not found")
    return DatabaseManager(db_config)


# -------------------------
# 键盘翻页（左右方向键）：multi 模式翻页，single 模式切换字段
# -------------------------
@callback(
    Output("multi-page-store", "data", allow_duplicate=True),
    Output("selected-fields-store", "data", allow_duplicate=True),
    Input("keyboard-prev-trigger", "n_clicks"),
    Input("keyboard-next-trigger", "n_clicks"),
    State("multi-page-store", "data"),
    State("multi-total-pages-store", "data"),
    State("view-mode-store", "data"),
    State("selected-fields-store", "data"),
    State("expanded-rule-store", "data"),
    prevent_initial_call=True,
)
def keyboard_navigation(prev_clicks, next_clicks, page_data, total_pages, view_mode, selected_fields, expanded_rule):
    trig = ctx.triggered_id

    # multi 模式：翻页
    if view_mode == "multi":
        page = int((page_data or {}).get("page", 1))
        total = int(total_pages or 1)

        if trig == "keyboard-prev-trigger":
            page = max(1, page - 1)
        elif trig == "keyboard-next-trigger":
            page = min(total, page + 1)

        return {"page": page}, no_update

    # single 模式：切换字段
    elif view_mode == "single":
        if not expanded_rule or not selected_fields:
            return no_update, no_update

        try:
            model = expanded_rule["model"]
            rule = expanded_rule["rule"]
            current_field_id = selected_fields[0]["field_id"]

            db = get_db_manager()
            fields = db.get_fields(model, rule)
            if not fields:
                return no_update, no_update

            field_ids = [f["field_id"] for f in fields]
            try:
                current_index = field_ids.index(current_field_id)
            except ValueError:
                return no_update, no_update

            if trig == "keyboard-prev-trigger":
                new_index = max(0, current_index - 1)
            elif trig == "keyboard-next-trigger":
                new_index = min(len(fields) - 1, current_index + 1)
            else:
                return no_update, no_update

            new_field_id = field_ids[new_index]
            new_selected = [{"model": model, "field_id": new_field_id}]
            return no_update, new_selected

        except Exception as e:
            print(f"键盘导航失败: {e}")
            return no_update, no_update

    return no_update, no_update


# -------------------------
# 填充 model dropdown
# -------------------------
@callback(
    Output("search-model-dropdown", "options"),
    Input("search-model-dropdown", "id"),
)
def populate_search_model_dropdown(_):
    """填充构型下拉框"""
    try:
        db = get_db_manager()
        models = db.get_models()
        return [{"label": m, "value": m} for m in models]
    except Exception as e:
        print(f"获取 model 列表失败: {e}")
        return []


# -------------------------
# Field 搜索功能
# -------------------------
@callback(
    Output("selected-fields-store", "data", allow_duplicate=True),
    Output("current-model-store", "data", allow_duplicate=True),
    Output("view-mode-store", "data", allow_duplicate=True),
    Input("field-search-btn", "n_clicks"),
    State("search-model-dropdown", "value"),
    State("field-search-input", "value"),
    prevent_initial_call=True,
)
def search_field(n_clicks, model, field_text):
    """搜索指定的 field"""
    if not n_clicks or not model or not field_text:
        return no_update, no_update, no_update

    field_text = field_text.strip()
    if not field_text:
        return no_update, no_update, no_update

    try:
        db = get_db_manager()
        schema = qident(model)

        query = f"""
        SELECT field_id, field, field_name, rule_code, type
        FROM {schema}.field
        WHERE field = %s
        """
        df = db.execute_query(query, (field_text,))

        if df.empty:
            print(f"[搜索] 未找到 field: {field_text} in model: {model}")
            return no_update, no_update, no_update

        row = df.iloc[0]
        field_info = {
            "model": model,
            "field_id": int(row["field_id"]),
            "field": row["field"],
            "field_name": row["field_name"] if row["field_name"] else row["field"],
            "rule_code": row["rule_code"],
            "type": row["type"],
        }

        print(f"[搜索] 找到 field: {field_text}, field_id: {field_info['field_id']}, model: {model}")
        return [field_info], model, "single"

    except Exception as e:
        import traceback
        print(f"[搜索] 失败: {e}")
        print(traceback.format_exc())
        return no_update, no_update, no_update


def _calc_time_range(value: str, recent_days: Optional[int] = None) -> Optional[Tuple[str, str]]:
    """计算时间范围
    
    Args:
        value: 时间范围类型（all/7d/30d/90d）
        recent_days: 当value为all时，可指定最近x天（基于系统当前时间）
    
    Returns:
        时间范围元组(start, end)，或None表示不限制
        
    注意：为了包含当天所有数据，使用 < 次日00:00:00 而不是 <= 当天23:59:59
    """
    if value == "all":
        # 只有在all模式下，recent_days才生效
        if recent_days and recent_days > 0:
            # 使用 < 次日00:00:00 来包含今天的所有数据
            end = datetime.now() + timedelta(days=1)
            end = end.replace(hour=0, minute=0, second=0, microsecond=0)
            start = end - timedelta(days=recent_days + 1)  # +1 因为end已经是次日
            return (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        return None
    
    # 使用 < 次日00:00:00 来包含今天的所有数据
    end = datetime.now() + timedelta(days=1)
    end = end.replace(hour=0, minute=0, second=0, microsecond=0)
    
    if value == "7d":
        start = end - timedelta(days=8)  # 8天前到明天 = 包含最近7天
    elif value == "30d":
        start = end - timedelta(days=31)
    elif value == "90d":
        start = end - timedelta(days=91)
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

    epi = df[["episode_id", "sn", "taskid", "area", "collected_at"]].drop_duplicates().copy()
    if not pd.api.types.is_datetime64_any_dtype(epi["collected_at"]):
        epi["collected_at"] = pd.to_datetime(epi["collected_at"], utc=True, errors="coerce")

    sort_keys: List[str] = []
    if sort_by == "sn":
        sort_keys.append("sn")
    elif sort_by == "taskid":
        sort_keys.append("taskid")
    elif sort_by == "area":
        sort_keys.append("area")
    else:
        sort_keys.append("collected_at")

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

    epi = epi.sort_values(sort_keys, kind="mergesort")
    epi["episode_index"] = range(1, len(epi) + 1)

    df = df.merge(epi[["episode_id", "episode_index"]], on="episode_id", how="left")
    return df


# -------------------------
# 视图模式切换
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
    if mode not in ("single", "multi"):
        mode = "single"
    return mode, [], None, {"page": 1}, None, None, None, None


# -------------------------
# 导航树渲染
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
                            html.Div(
                                [],
                                id={"type": "field-list", "model": model, "rule": rule_code},
                                style={"display": "none"},
                            ),
                        ]
                    )
                )

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
# single：点击 rule 展开字段（懒加载）
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
    n = len(rule_ids) if rule_ids else 0

    def _no_updates():
        return [no_update] * n, [no_update] * n, no_update

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
# single：字段选择
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
# multi：点击 rule 选择 rule + 重置页码
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
    return {"model": model, "rule": rule}, {"page": 1}, [], model


# -------------------------
# multi：分页字段
# -------------------------
@callback(
    Output("selected-fields-store", "data", allow_duplicate=True),
    Output("multi-total-pages-store", "data"),
    Output("multi-pagination-container", "style"),
    Output("multi-page-indicator", "children"),
    Input("selected-rule-store", "data"),
    Input("multi-page-store", "data"),
    State("view-mode-store", "data"),
    prevent_initial_call=True,
)
def update_multi_page_fields(selected_rule, page_data, view_mode):
    if view_mode != "multi":
        return [], 1, {"display": "none"}, ""

    if not selected_rule:
        return [], 1, {"display": "none"}, ""

    model = selected_rule["model"]
    rule = selected_rule["rule"]
    page = int((page_data or {}).get("page", 1))

    db = get_db_manager()
    fields = db.get_fields(model, rule)

    total_fields = len(fields)
    total_pages = max(1, math.ceil(total_fields / MULTI_PAGE_SIZE))
    page = max(1, min(page, total_pages))

    start = (page - 1) * MULTI_PAGE_SIZE
    end = min(total_fields, start + MULTI_PAGE_SIZE)
    page_fields = fields[start:end]

    selected_fields = [{"model": model, "field_id": int(f["field_id"])} for f in page_fields]
    indicator = f"第 {page}/{total_pages} 页（本页 {len(page_fields)} / 总字段 {total_fields}）"

    return selected_fields, total_pages, {"display": "block"}, indicator


# -------------------------
# 已选信息显示（保持你原逻辑）
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

    info_items.append(html.Hr())

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

    if episode_id_val:
        info_items.append(
            html.Div(
                [
                    dbc.Button(
                        [html.I(className="bi bi-download me-2"), "下载 JSON"],
                        id="download-json-btn",
                        color="primary",
                        size="sm",
                        className="mt-2",
                    )
                ],
                className="mt-2",
            )
        )

    return html.Div(info_items, className="p-2 bg-light rounded")


# -------------------------
# 图表更新：✅修复 missing 统计
# -------------------------
@callback(
    Output("chart-container", "children"),
    Output("statistics-panel", "children"),
    Input("selected-fields-store", "data"),
    Input("time-range-dropdown", "value"),
    Input("sort-by-dropdown", "value"),
    Input("group-by-dropdown", "value"),
    Input("sampling-active-store", "data"),
    Input("filtered-areas-store", "data"),
    Input("recent-days-store", "data"),
    State("view-mode-store", "data"),
    State("selected-rule-store", "data"),
    State("sampled-episodes-store", "data"),
)
def update_chart(selected_fields, time_range, sort_by, group_by, sampling_active, filtered_areas, recent_days, view_mode, selected_rule, sampled_episodes):
    if not selected_fields:
        if view_mode == "multi":
            return html.Div("请从左侧导航树选择 Rule Code", className="text-muted text-center mt-5"), None
        return html.Div("请从左侧导航树选择字段", className="text-muted text-center mt-5"), None

    try:
        t0 = time.perf_counter()

        db = get_db_manager()
        model = selected_fields[0]["model"]
        field_ids = [int(f["field_id"]) for f in selected_fields]

        time_range_tuple = _calc_time_range(time_range, recent_days)

        # 拉数据
        t_db0 = time.perf_counter()
        df = db.get_field_data(model, field_ids, time_range_tuple)
        t_db1 = time.perf_counter()

        # 抽样过滤（对原始 df 做）
        if sampling_active and sampled_episodes:
            original_len = len(df)
            df = df[df["episode_id"].isin(sampled_episodes)]
            print(f"[抽样过滤] 原始数据: {original_len}, 过滤后: {len(df)}")

        # 地区过滤（对原始 df 做）
        if filtered_areas and len(filtered_areas) > 0:
            original_len = len(df)
            df = df[~df["area"].isin(filtered_areas)]
            print(f"[地区过滤] 过滤掉 {filtered_areas}, 原始数据: {original_len}, 过滤后: {len(df)}")

        if df.empty:
            return html.Div("未找到符合条件的数据", className="text-muted text-center mt-5"), None

        # meta & thresholds
        t_meta0 = time.perf_counter()
        field_info_map = db.get_field_info_batch(model, field_ids)
        fields_for_thresholds = [field_info_map[fid]["field"] for fid in field_ids if fid in field_info_map]
        thresholds_map = db.get_thresholds_batch(model, fields_for_thresholds)
        t_meta1 = time.perf_counter()

        # ✅ 保留 raw
        df["value_raw"] = df["value"]

        # ✅ 新：生成 value_num（画图/统计都用它，但统计的 missing 用 value_raw 判 NULL）
        def to_value_num(row):
            fid = row["field_id"]
            if fid not in field_info_map:
                return pd.NA

            field_type = field_info_map[fid].get("type", "numeric")
            v = row["value_raw"]

            if field_type == "non_numeric":
                # non_numeric: 缺失(空/None/N/A/null/nan) -> 1.0, 有值 -> 0.0
                if pd.isna(v):
                    return 1.0
                s = str(v).strip()
                if s == "" or s.lower() in ("n/a", "null", "nan"):
                    return 1.0
                return 0.0
            else:
                try:
                    return float(v)
                except (ValueError, TypeError):
                    return pd.NA

        t_conv0 = time.perf_counter()
        df["value_num"] = df.apply(to_value_num, axis=1)
        t_conv1 = time.perf_counter()

        # ✅ 图表用 df_plot：只过滤 value_num，不动 df（df 要留给统计）
        df_plot = df.dropna(subset=["value_num"]).copy()
        if df_plot.empty:
            return html.Div("未找到可用的数值数据（value 全部无法转为数值）", className="text-muted text-center mt-5"), None

        # field_info_list
        field_info_list = []
        for fid in field_ids:
            if fid not in field_info_map:
                continue
            info = field_info_map[fid]
            field_info_list.append(
                {
                    "field_id": fid,
                    "field_name": info["display_name"],
                    "thresholds": thresholds_map.get(info["field"]),
                    "type": info.get("type", "numeric"),
                }
            )

        # multi：episode_index 用 df_plot（保证 x 轴只针对画出来的点）
        t_epi0 = time.perf_counter()
        if view_mode == "multi":
            df_plot = _compute_global_episode_index(df_plot, sort_by=sort_by, group_by=group_by)
        t_epi1 = time.perf_counter()

        # 图表
        t_fig0 = time.perf_counter()
        if view_mode == "multi":
            chart = create_scatter_plot_multi_subplots(
                df_plot,
                field_info_list,
                cols=3,
                sort_by=sort_by,
                group_by=group_by,
            )
        else:
            chart = create_scatter_plot(df_plot, field_info_list, sort_by, group_by)
        t_fig1 = time.perf_counter()

        # ✅ 统计：用完整 df（包含 value_raw NULL 的行）
        t_stats0 = time.perf_counter()
        stats = create_statistics_cards(df, field_info_list, group_by)
        t_stats1 = time.perf_counter()

        # figure 大小 / trace 数
        fig_bytes_mb = None
        trace_count = None
        try:
            fig_json = chart.figure.to_plotly_json()
            fig_bytes_mb = len(_json.dumps(fig_json, ensure_ascii=False)) / (1024 * 1024)
            trace_count = len(chart.figure.data)
        except Exception:
            pass

        t1 = time.perf_counter()

        print(
            "[perf] "
            f"model={model} mode={view_mode} fields={len(field_ids)} "
            f"rows_plot={len(df_plot)} rows_raw={len(df)} "
            f"db={t_db1 - t_db0:.3f}s "
            f"meta={t_meta1 - t_meta0:.3f}s "
            f"convert={t_conv1 - t_conv0:.3f}s "
            f"episode_index={t_epi1 - t_epi0:.3f}s "
            f"figure={t_fig1 - t_fig0:.3f}s "
            f"stats={t_stats1 - t_stats0:.3f}s "
            f"total={t1 - t0:.3f}s "
            f"traces={trace_count} "
            f"figMB={fig_bytes_mb}"
        )

        return chart, stats

    except Exception as e:
        return html.Div(f"图表生成失败: {str(e)}", className="text-danger text-center mt-5"), None


@callback(
    Output("selected-episode-store", "data"),
    Input("field-chart", "clickData"),
    prevent_initial_call=True,
)
def capture_click_data(click_data):
    """捕获图表点击事件（multi 子图也适用）"""
    if not click_data or "points" not in click_data or not click_data["points"]:
        return no_update

    point = click_data["points"][0]
    if "customdata" in point:
        customdata = point["customdata"]
        if isinstance(customdata, list) and len(customdata) >= 2:
            return {"episode_id": customdata[0], "field_id": customdata[1]}
        else:
            return {"episode_id": customdata}

    return no_update


@callback(
    Output("filtered-areas-store", "data"),
    Input("area-filter-checklist", "value"),
)
def handle_area_filter(selected_areas):
    """处理地区过滤：排除选中的地区"""
    if not selected_areas:
        return []
    print(f"[地区过滤] 排除的地区: {selected_areas}")
    return selected_areas


@callback(
    Output("recent-days-store", "data"),
    Input("recent-days-input", "value"),
)
def handle_recent_days(days):
    """处理最近x天的输入：同步到Store"""
    if days and days > 0:
        print(f"[最近天数] 设置为: {days} 天")
        return int(days)
    return None


@callback(
    Output("sampled-episodes-store", "data"),
    Output("sampling-active-store", "data"),
    Input("sampling-ratio-dropdown", "value"),
    State("current-model-store", "data"),
    State("selected-fields-store", "data"),
)
def handle_sampling(ratio, current_model, selected_fields):
    """
    处理抽样：监听下拉菜单值变化
    - 100%: 不抽样（返回 None, False）
    - 其他: 按 taskid 分组抽样，每个 taskid 至少保留 1 个 episode
    """
    import random

    if not ratio or ratio >= 100:
        return None, False

    if not current_model or not selected_fields:
        return None, False

    try:
        db = get_db_manager()
        schema = qident(current_model)

        query = f"""
        SELECT DISTINCT episode_id, taskid
        FROM {schema}.episode
        ORDER BY taskid, episode_id
        """
        df = db.execute_query(query)

        if df.empty:
            return None, False

        sampled_episodes = []
        ratio_decimal = ratio / 100.0

        for taskid in df["taskid"].unique():
            taskid_episodes = df[df["taskid"] == taskid]["episode_id"].tolist()
            sample_size = max(1, int(len(taskid_episodes) * ratio_decimal))
            sampled = random.sample(taskid_episodes, sample_size)
            sampled_episodes.extend(sampled)

        print(f"[抽样] 总 episode 数: {len(df)}, 抽样后: {len(sampled_episodes)}, 比例: {ratio}%")
        return sampled_episodes, True

    except Exception as e:
        print(f"抽样失败: {e}")
        import traceback
        print(traceback.format_exc())
        return None, False


@callback(
    Output("download-json", "data"),
    Input("download-json-btn", "n_clicks"),
    State("selected-episode-store", "data"),
    State("current-model-store", "data"),
    prevent_initial_call=True,
)
def download_json(n_clicks, episode_data, current_model):
    import json

    if not n_clicks or not episode_data or not current_model:
        return no_update

    episode_id = episode_data.get("episode_id")
    if not episode_id:
        return no_update

    try:
        db = get_db_manager()
        json_data = db.get_collect_json(current_model, episode_id)

        if not json_data:
            print(f"[download_json] 未找到 JSON 数据: episode_id={episode_id}, model={current_model}")
            return no_update

        json_content = json_data.get("json")
        filename = json_data.get("filename", f"{episode_id}_collect.json")

        if not filename.endswith(".json"):
            filename = f"{episode_id}_collect.json"

        if isinstance(json_content, (dict, list)):
            json_str = json.dumps(json_content, ensure_ascii=False, indent=2)
        elif isinstance(json_content, str):
            json_str = json_content
        else:
            json_str = str(json_content)

        return {"content": json_str, "filename": filename}

    except Exception as e:
        import traceback
        print(f"下载 JSON 失败: {e}")
        print(traceback.format_exc())
        return no_update
