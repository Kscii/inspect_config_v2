# src/visualize_db_app/layout.py
"""
Dash 页面布局
"""

from dash import dcc, html
import dash_bootstrap_components as dbc


def create_layout():
    """创建页面布局"""
    return dbc.Container(
        [
            # 标题行
            dbc.Row(
                [
                    dbc.Col(
                        html.H1("Inspect Config Visualizer", className="text-center mb-4"),
                        width=12,
                    )
                ]
            ),
            # 主内容区
            dbc.Row(
                [
                    # 左侧导航树
                    dbc.Col(
                        [
                            html.H5("导航树", className="mb-3"),
                            dcc.Loading(
                                id="loading-tree",
                                type="default",
                                children=[
                                    html.Div(
                                        id="navigation-tree",
                                        style={"overflowY": "auto", "maxHeight": "1000px"},
                                    )
                                ],
                            ),
                        ],
                        width=2,
                        style={"borderRight": "1px solid #dee2e6", "paddingRight": "20px"},
                    ),
                    # 右侧图表区
                    dbc.Col(
                        [
                            # 控制面板
                            dbc.Card(
                                [
                                    dbc.CardBody(
                                        [
                                            # 视图模式选择
                                            dbc.Row(
                                                [
                                                    dbc.Col(
                                                        [
                                                            html.Label("视图模式："),
                                                            dbc.RadioItems(
                                                                id="view-mode-radio",
                                                                options=[
                                                                    {"label": "单图模式", "value": "single"},
                                                                    {"label": "多图模式", "value": "multi"},
                                                                ],
                                                                value="single",
                                                                inline=True,
                                                            ),
                                                        ],
                                                        width=12,
                                                    )
                                                ],
                                                className="mb-2",
                                            ),

                                            # 时间范围、排序和分类选择
                                            dbc.Row(
                                                [
                                                    dbc.Col(
                                                        [
                                                            html.Label("时间范围："),
                                                            dcc.Dropdown(
                                                                id="time-range-dropdown",
                                                                options=[
                                                                    {"label": "全部", "value": "all"},
                                                                    {"label": "最近7天", "value": "7d"},
                                                                    {"label": "最近30天", "value": "30d"},
                                                                    {"label": "最近90天", "value": "90d"},
                                                                ],
                                                                value="all",
                                                                clearable=False,
                                                            ),
                                                        ],
                                                        width=4,
                                                    ),
                                                    dbc.Col(
                                                        [
                                                            html.Label("排序方式："),
                                                            dcc.Dropdown(
                                                                id="sort-by-dropdown",
                                                                options=[
                                                                    {"label": "按时间排序", "value": "time"},
                                                                    {"label": "按SN排序", "value": "sn"},
                                                                    {"label": "按TaskID排序", "value": "taskid"},
                                                                    {"label": "按地区排序", "value": "area"},
                                                                ],
                                                                value="time",
                                                                clearable=False,
                                                            ),
                                                        ],
                                                        width=4,
                                                    ),
                                                    dbc.Col(
                                                        [
                                                            html.Label("分类方式："),
                                                            dcc.Dropdown(
                                                                id="group-by-dropdown",
                                                                options=[
                                                                    {"label": "按SN分类", "value": "sn"},
                                                                    {"label": "按TaskID分类", "value": "taskid"},
                                                                    {"label": "按地区分类", "value": "area"},
                                                                    {"label": "按日分类", "value": "day"},
                                                                    {"label": "按周分类", "value": "week"},
                                                                    {"label": "按月分类", "value": "month"},
                                                                ],
                                                                value="sn",
                                                                clearable=False,
                                                            ),
                                                        ],
                                                        width=4,
                                                    ),
                                                ],
                                                className="mb-3",
                                            ),

                                            # multi 模式分页控件（由回调控制显隐/禁用）
                                            html.Div(
                                                id="multi-pagination-container",
                                                children=[
                                                    dbc.Button(
                                                        "上一页",
                                                        id="multi-prev-btn",
                                                        color="secondary",
                                                        outline=True,
                                                        size="sm",
                                                        className="me-2",
                                                    ),
                                                    html.Span(
                                                        id="multi-page-indicator",
                                                        className="text-muted small me-2",
                                                    ),
                                                    dbc.Button(
                                                        "下一页",
                                                        id="multi-next-btn",
                                                        color="secondary",
                                                        outline=True,
                                                        size="sm",
                                                    ),
                                                ],
                                                style={"display": "none"},
                                                className="mb-3",
                                            ),

                                            # 已选信息显示（字段 + Episode）
                                            dbc.Row(
                                                [
                                                    dbc.Col(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.Label("已选信息："),
                                                                    html.Div(
                                                                        id="selected-info-display",
                                                                        className="mb-2",
                                                                    ),
                                                                ]
                                                            ),
                                                        ],
                                                        width=12,
                                                    ),
                                                ],
                                                className="mb-3",
                                            ),
                                        ]
                                    )
                                ],
                                className="mb-4",
                            ),

                            # 图表区
                            dcc.Loading(
                                id="loading-chart",
                                type="default",
                                children=[html.Div(id="chart-container")],
                            ),
                            # 统计面板
                            html.Div(id="statistics-panel", className="mt-4"),
                        ],
                        width=9,
                    ),
                ],
            ),

            # 隐藏的存储组件
            dcc.Store(id="view-mode-store", data="single"),
            dcc.Store(id="selected-fields-store", data=[]),  # 存储已选字段列表
            dcc.Store(id="current-model-store"),  # 当前选中的 model
            dcc.Store(id="expanded-model-store", data=None),  # 当前展开的 model（字符串或 None）
            dcc.Store(id="expanded-rule-store", data=None),  # 当前展开的 rule（{"model": "...", "rule": "..."} 或 None）
            dcc.Store(id="selected-episode-store", data=None),  # 当前选中的 episode 信息

            # multi 模式专用
            dcc.Store(id="selected-rule-store", data=None),  # {"model":..., "rule":...}
            dcc.Store(id="multi-page-store", data={"page": 1}),
            dcc.Store(id="multi-total-pages-store", data=1),
        ],
        fluid=True,
        className="p-4",
    )
