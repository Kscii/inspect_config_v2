# src/visualize_db_app/callbacks.py
"""
Dash 回调函数
"""

from datetime import datetime, timedelta
from typing import List, Dict, Any

import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, callback, html, ctx, no_update, ALL
import dash_bootstrap_components as dbc

from .app import app
from .database import DatabaseManager
from .charts import create_scatter_plot, create_statistics_cards


def get_db_manager() -> DatabaseManager:
    """获取数据库管理器实例"""
    db_config = app.server.config.get("DB_CONFIG")
    if not db_config:
        raise ValueError("Database configuration not found")
    return DatabaseManager(db_config)


@callback(
    Output("navigation-tree", "children"),
    Input("navigation-tree", "id"),  # 触发初始加载
)
def render_navigation_tree(_):
    """渲染导航树"""
    try:
        db = get_db_manager()
        models = db.get_models()  # 返回小写的 schema 名称，如 'a2', 'gr2'
        
        if not models:
            return html.Div("未找到任何模型", className="text-muted")
        
        tree_items = []
        for model in models:
            # model 是小写的 schema 名称（如 'a2'），用于数据库查询
            # 显示时也使用原始名称（保持和数据库一致）
            model_display = model
            
            # 获取该 model 下的所有 rule_code
            rule_codes = db.get_rule_codes(model)
            
            rule_items = []
            for rule_code in rule_codes:
                # 获取该 rule_code 下的所有字段
                fields = db.get_fields(model, rule_code)
                
                field_items = [
                    dbc.ListGroupItem(
                        f["field"],
                        id={"type": "field-item", "model": model, "rule": rule_code, "field_id": f["field_id"]},
                        action=True,
                        className="ps-5",
                        style={"fontSize": "0.85rem"},
                    )
                    for f in fields
                ]
                
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
                            html.Div(field_items, id={"type": "field-list", "model": model, "rule": rule_code}, style={"display": "none"}),
                        ]
                    )
                )
            
            tree_items.append(
                html.Div(
                    [
                        dbc.ListGroupItem(
                            f"🤖 {model_display}",  # 显示大写
                            id={"type": "model-item", "model": model},  # 存储小写
                            action=True,
                            color="primary",
                            className="fw-bold",
                        ),
                        html.Div(rule_items, id={"type": "rule-list", "model": model}, style={"display": "none"}),
                    ],
                    className="mb-2",
                )
            )
        
        return dbc.ListGroup(tree_items, flush=True)
    
    except Exception as e:
        return html.Div(f"加载导航树失败: {str(e)}", className="text-danger")


@callback(
    Output("expanded-model-store", "data"),
    Output({"type": "rule-list", "model": ALL}, "style"),
    Input({"type": "model-item", "model": ALL}, "n_clicks"),
    State("expanded-model-store", "data"),
    State({"type": "model-item", "model": ALL}, "id"),
    prevent_initial_call=True,
)
def toggle_model(n_clicks_list, expanded_model, model_ids):
    """展开/折叠 Model 下的 Rule Code 列表"""
    if not ctx.triggered:
        return no_update, no_update
    
    # 找到被点击的 model
    triggered_id = ctx.triggered_id
    if not triggered_id or "model" not in triggered_id:
        return no_update, no_update
    
    triggered_model = triggered_id["model"]
    
    # 切换逻辑：如果当前就是展开的 model，设为 None（折叠）；否则设为这个 model
    new_expanded_model = None if expanded_model == triggered_model else triggered_model
    
    # 根据新状态设置所有 rule-list 的显示状态
    new_styles = []
    for model_id in model_ids:
        model = model_id["model"]
        if model == new_expanded_model:
            new_styles.append({"display": "block"})
        else:
            new_styles.append({"display": "none"})
    
    return new_expanded_model, new_styles


@callback(
    Output("expanded-rule-store", "data"),
    Output({"type": "field-list", "model": ALL, "rule": ALL}, "style"),
    Input({"type": "rule-item", "model": ALL, "rule": ALL}, "n_clicks"),
    State("expanded-rule-store", "data"),
    State({"type": "rule-item", "model": ALL, "rule": ALL}, "id"),
    prevent_initial_call=True,
)
def toggle_rule(n_clicks_list, expanded_rule, rule_ids):
    """展开/折叠 Rule Code 下的 Field 列表"""
    if not ctx.triggered:
        return no_update, no_update
    
    # 找到被点击的 rule
    triggered_id = ctx.triggered_id
    if not triggered_id or "rule" not in triggered_id:
        return no_update, no_update
    
    triggered_model = triggered_id["model"]
    triggered_rule = triggered_id["rule"]
    triggered_info = {"model": triggered_model, "rule": triggered_rule}
    
    # 切换逻辑：如果当前就是展开的 rule，设为 None（折叠）；否则设为这个 rule
    new_expanded_rule = None if expanded_rule == triggered_info else triggered_info
    
    # 根据新状态设置所有 field-list 的显示状态
    new_styles = []
    for rule_id in rule_ids:
        if new_expanded_rule and rule_id["model"] == new_expanded_rule["model"] and rule_id["rule"] == new_expanded_rule["rule"]:
            new_styles.append({"display": "block"})
        else:
            new_styles.append({"display": "none"})
    
    return new_expanded_rule, new_styles


@callback(
    Output("selected-fields-store", "data"),
    Output("current-model-store", "data"),
    Input({"type": "field-item", "model": ALL, "rule": ALL, "field_id": ALL}, "n_clicks"),
    State("selected-fields-store", "data"),
    State("current-model-store", "data"),
    prevent_initial_call=True,
)
def select_field(n_clicks_list, selected_fields, current_model):
    """处理字段选择（单图模式）"""
    if not ctx.triggered:
        return no_update, no_update
    
    triggered_id = ctx.triggered_id
    if not triggered_id:
        return no_update, no_update
    
    model = triggered_id["model"]
    field_id = triggered_id["field_id"]
    
    # 初始化
    if selected_fields is None:
        selected_fields = []
    
    # 检查是否切换了 model
    if current_model and current_model != model:
        # 切换 model，清空之前的选择
        selected_fields = []
    
    # 构建字段信息
    field_info = {"model": model, "field_id": field_id}
    
    # 单图模式：只保留一个字段，新选择替换旧的
    if selected_fields and selected_fields[0] == field_info:
        # 点击同一个字段，取消选择
        selected_fields = []
    else:
        # 替换为新选择的字段
        selected_fields = [field_info]
    
    return selected_fields, model


@callback(
    Output("selected-info-display", "children"),
    Input("selected-fields-store", "data"),
    Input("selected-episode-store", "data"),
    State("current-model-store", "data"),
)
def update_selected_info_display(selected_fields, episode_data, current_model):
    """更新已选信息显示（字段 + Episode）"""
    info_items = []
    
    # 显示已选字段
    if selected_fields:
        db = get_db_manager()
        field_badges = []
        for field_info in selected_fields:
            field_name = db.get_field_name(field_info["model"], field_info["field_id"])
            field_badges.append(
                dbc.Badge(
                    field_name,
                    color="primary",
                    className="me-2 mb-2",
                    style={"fontSize": "0.85rem"},
                )
            )
        info_items.append(
            html.Div([
                html.Strong("已选字段： "),
                html.Div(field_badges, style={"display": "inline-block"}),
            ], className="mb-2")
        )
    else:
        info_items.append(
            html.Div([
                html.Strong("已选字段： "),
                html.Span("尚未选择字段", className="text-muted"),
            ], className="mb-2")
        )
    
    # 显示 Episode 信息
    info_items.append(html.Hr())
    
    if episode_data and current_model:
        try:
            from .database import qident
            
            db = get_db_manager()
            episode_id = episode_data.get("episode_id")
            
            if episode_id:
                # 获取episode详细信息
                schema = qident(current_model)
                query = f"""
                SELECT episode_id, taskid, model, area, sn, collected_at, filename
                FROM {schema}.episode
                WHERE episode_id = %s
                """
                df = db.execute_query(query, (episode_id,))
                
                if not df.empty:
                    episode_info = df.iloc[0]
                    info_items.extend([
                        html.Div([html.Strong("Episode ID: "), html.Span(str(episode_info["episode_id"]))], className="mb-1"),
                        html.Div([html.Strong("SN: "), html.Span(str(episode_info["sn"]))], className="mb-1"),
                        html.Div([html.Strong("TaskID: "), html.Span(str(episode_info["taskid"]))], className="mb-1"),
                        html.Div([html.Strong("采集地点: "), html.Span(str(episode_info.get("area", "N/A")))], className="mb-1"),
                        html.Div([html.Strong("采集时间: "), html.Span(str(episode_info["collected_at"]))], className="mb-1"),
                        html.Div([html.Strong("文件名: "), html.Span(str(episode_info["filename"]))], className="mb-1"),
                    ])
                else:
                    # 未找到信息，显示空内容
                    info_items.extend([
                        html.Div([html.Strong("Episode ID: "), html.Span("")], className="mb-1"),
                        html.Div([html.Strong("SN: "), html.Span("")], className="mb-1"),
                        html.Div([html.Strong("TaskID: "), html.Span("")], className="mb-1"),
                        html.Div([html.Strong("采集时间: "), html.Span("")], className="mb-1"),
                        html.Div([html.Strong("文件名: "), html.Span("")], className="mb-1"),
                    ])
        except Exception as e:
            info_items.extend([
                html.Div([html.Strong("Episode ID: "), html.Span("")], className="mb-1"),
                html.Div([html.Strong("SN: "), html.Span("")], className="mb-1"),
                html.Div([html.Strong("TaskID: "), html.Span("")], className="mb-1"),
                html.Div([html.Strong("采集时间: "), html.Span("")], className="mb-1"),
                html.Div([html.Strong("文件名: "), html.Span("")], className="mb-1"),
                html.Div([html.Span(f"错误: {str(e)}", className="text-danger small")], className="mb-1"),
            ])
    else:
        # 未选中 episode，显示空内容
        info_items.extend([
            html.Div([html.Strong("Episode ID: "), html.Span("")], className="mb-1"),
            html.Div([html.Strong("SN: "), html.Span("")], className="mb-1"),
            html.Div([html.Strong("TaskID: "), html.Span("")], className="mb-1"),
            html.Div([html.Strong("采集地点: "), html.Span("")], className="mb-1"),
            html.Div([html.Strong("采集时间: "), html.Span("")], className="mb-1"),
            html.Div([html.Strong("文件名: "), html.Span("")], className="mb-1"),
        ])
    
    return html.Div(info_items, className="p-2 bg-light rounded")


@callback(
    Output("chart-container", "children"),
    Output("statistics-panel", "children"),
    Input("selected-fields-store", "data"),
    Input("time-range-dropdown", "value"),
    Input("sort-by-dropdown", "value"),
    Input("group-by-dropdown", "value"),
)
def update_chart(selected_fields, time_range, sort_by, group_by):
    """更新图表和统计面板"""
    if not selected_fields:
        return html.Div("请从左侧导航树选择字段", className="text-muted text-center mt-5"), None
    
    try:
        db = get_db_manager()
        model = selected_fields[0]["model"]
        field_ids = [f["field_id"] for f in selected_fields]
        
        # 计算时间范围
        time_range_tuple = None
        if time_range == "7d":
            end = datetime.now()
            start = end - timedelta(days=7)
            time_range_tuple = (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        elif time_range == "30d":
            end = datetime.now()
            start = end - timedelta(days=30)
            time_range_tuple = (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        elif time_range == "90d":
            end = datetime.now()
            start = end - timedelta(days=90)
            time_range_tuple = (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        
        # 获取数据
        df = db.get_field_data(model, field_ids, time_range_tuple)
        
        if df.empty:
            return html.Div("未找到符合条件的数据", className="text-muted text-center mt-5"), None
        
        # 转换 value 为数值类型
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna(subset=["value"])
        
        # 获取字段名称和阈值
        field_info_list = []
        for field_id in field_ids:
            field_name = db.get_field_name(model, field_id)
            thresholds = db.get_thresholds(model, field_name)
            field_info_list.append({
                "field_id": field_id,
                "field_name": field_name,
                "thresholds": thresholds,
            })
        
        # 创建图表
        chart = create_scatter_plot(df, field_info_list, sort_by, group_by)
        
        # 创建统计面板
        stats = create_statistics_cards(df, field_info_list)
        
        return chart, stats
    
    except Exception as e:
        return html.Div(f"图表生成失败: {str(e)}", className="text-danger text-center mt-5"), None


@callback(
    Output("selected-episode-store", "data"),
    Input("field-chart", "clickData"),
    prevent_initial_call=True,
)
def capture_click_data(click_data):
    """捕获图表点击事件"""
    if not click_data or "points" not in click_data or not click_data["points"]:
        return no_update
    
    # 获取点击的点的数据
    point = click_data["points"][0]
    
    # customdata 包含 episode_id
    if "customdata" in point:
        episode_id = point["customdata"]
        return {"episode_id": episode_id}
    
    return no_update
