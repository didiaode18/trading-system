"""
归因模块
========
Barra风格因子归因、绩效分解、Alpha/Beta分离
"""

# FIX: 使用相对导入，支持 attribution 与 trading_system.attribution 两种导入方式
from .barra import BarraAttribution

try:
    from .trade_log import TradeLog
    from .alpha_beta import calc_alpha_beta_attribution
except ImportError:
    TradeLog = None
    calc_alpha_beta_attribution = None

try:
    from .brinson import brinson_attribution, build_sector_maps, render_brinson_section_html
    from .sharpe_decompose import decompose_sharpe, render_sharpe_decompose_html
except ImportError:
    brinson_attribution = None
    build_sector_maps = None
    render_brinson_section_html = None
    decompose_sharpe = None
    render_sharpe_decompose_html = None

__all__ = [
    "BarraAttribution", "TradeLog", "calc_alpha_beta_attribution",
    "brinson_attribution", "build_sector_maps", "render_brinson_section_html",
    "decompose_sharpe", "render_sharpe_decompose_html",
]
