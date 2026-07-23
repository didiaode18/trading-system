"""
归因模块
========
Barra风格因子归因、绩效分解、Alpha/Beta分离
"""

from attribution.barra import BarraAttribution

try:
    from attribution.trade_log import TradeLog
    from attribution.alpha_beta import calc_alpha_beta_attribution
except ImportError:
    TradeLog = None
    calc_alpha_beta_attribution = None

__all__ = ["BarraAttribution", "TradeLog", "calc_alpha_beta_attribution"]
