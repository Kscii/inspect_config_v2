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
                            
                            # Field 搜索功能
                            dbc.Card(
                                [
                                    dbc.CardBody(
                                        [
                                            html.Label("Field 搜索：", className="fw-bold mb-2"),
                                            dcc.Dropdown(
                                                id="search-model-dropdown",
                                                placeholder="选择构型...",
                                                clearable=True,
                                                style={"marginBottom": "8px"},
                                            ),
                                            dbc.InputGroup(
                                                [
                                                    dbc.Input(
                                                        id="field-search-input",
                                                        placeholder="输入完整 field 名称...",
                                                        type="text",
                                                    ),
                                                    dbc.Button(
                                                        "搜索",
                                                        id="field-search-btn",
                                                        color="primary",
                                                    ),
                                                ],
                                                size="sm",
                                            ),
                                        ],
                                        className="p-2",
                                    ),
                                ],
                                className="mb-3",
                            ),
                            
                            dcc.Loading(
                                id="loading-tree",
                                type="default",
                                children=[
                                    html.Div(
                                        id="navigation-tree",
                                        style={"overflowY": "auto", "maxHeight": "800px"},
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
                                            # 视图模式
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
                                                        width=6,
                                                    ),
                                                    dbc.Col(
                                                        [
                                                            html.Label("抽样比例："),
                                                            dcc.Dropdown(
                                                                id="sampling-ratio-dropdown",
                                                                options=[
                                                                    {"label": "1%", "value": 1},
                                                                    {"label": "5%", "value": 5},
                                                                    {"label": "10%", "value": 10},
                                                                    {"label": "20%", "value": 20},
                                                                    {"label": "35%", "value": 35},
                                                                    {"label": "50%", "value": 50},
                                                                    {"label": "100%", "value": 100},
                                                                ],
                                                                value=10,
                                                                clearable=False,
                                                            ),
                                                        ],
                                                        width=6,
                                                    ),
                                                ],
                                                className="mb-2",
                                            ),

                                            # 地区过滤器
                                            dbc.Row(
                                                [
                                                    dbc.Col(
                                                        [
                                                            html.Label("过滤地区："),
                                                            dcc.Checklist(
                                                                id="area-filter-checklist",
                                                                options=[
                                                                    {"label": " shanghai_dev", "value": "shanghai_dev"},
                                                                    {"label": " shanghai_prod", "value": "shanghai_prod"},
                                                                    {"label": " zhengzhou_prod", "value": "zhengzhou_prod"},
                                                                ],
                                                                value=["shanghai_dev"],  # 初始选中 shanghai_dev（排除）
                                                                inline=True,
                                                                className="mb-2",
                                                                labelStyle={"marginRight": "15px"},
                                                            ),
                                                        ],
                                                        width=12,
                                                    ),
                                                ],
                                                className="mb-3",
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
                                                            html.Div(
                                                                [
                                                                    html.Label("最近天数（仅all时生效）：", className="mt-2 small text-muted"),
                                                                    dbc.Input(
                                                                        id="recent-days-input",
                                                                        type="number",
                                                                        placeholder="留空=不限制",
                                                                        min=1,
                                                                        step=1,
                                                                        size="sm",
                                                                    ),
                                                                ]
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
                                                                value="sn",
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

                                            # multi 模式分页信息显示
                                            html.Div(
                                                id="multi-pagination-container",
                                                children=[
                                                    html.Span(
                                                        id="multi-page-indicator",
                                                        className="text-muted small",
                                                    ),
                                                    html.Span(
                                                        " （使用 ← → 方向键翻页）",
                                                        className="text-muted small ms-2",
                                                    ),
                                                ],
                                                style={"display": "none"},
                                                className="mb-3",
                                            ),
                                            # 键盘监听触发器（隐藏）
                                            html.Button(
                                                id="keyboard-prev-trigger",
                                                style={"display": "none"},
                                            ),
                                            html.Button(
                                                id="keyboard-next-trigger",
                                                style={"display": "none"},
                                            ),
                                            html.Div(
                                                id="keyboard-listener-output",
                                                style={"display": "none"},
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
            
            # 抽样相关
            dcc.Store(id="sampled-episodes-store", data=None),  # 存储抽样的 episode_id 列表
            dcc.Store(id="sampling-active-store", data=False),  # 是否启用抽样
            
            # 地区过滤相关
            dcc.Store(id="filtered-areas-store", data=[]),  # 存储需要过滤的地区列表
            
            # 最近x天相关
            dcc.Store(id="recent-days-store", data=None),  # 存储最近天数值
            
            # 下载组件
            dcc.Download(id="download-json"),
        ],
        fluid=True,
        className="p-4",
    )
