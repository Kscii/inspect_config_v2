# src/visualize_db_app/app.py
"""
Dash 应用主入口
"""

import dash
import dash_bootstrap_components as dbc

# 创建 Dash 应用
app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    suppress_callback_exceptions=True,
)

app.title = "Inspect Config Visualizer"

# 导入布局和回调（延迟导入避免循环依赖）
from . import layout, callbacks

app.layout = layout.create_layout()
