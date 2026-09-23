"""cli.auto_market 市场路由测试

验证股票代码 → 市场映射, 重点覆盖北交所路由修复:
- 修复前: 43/83/87 开头全部误判为深圳, 920 开头误判为上海
"""

import pytest

from tdxrs import MARKET_BJ, MARKET_SH, MARKET_SZ
from tdxrs.cli import auto_market


@pytest.mark.parametrize("code", ["600519", "601318", "688001"])
def test_sh_stocks(code):
    assert auto_market(code) == MARKET_SH


@pytest.mark.parametrize("code", ["510300", "588000"])
def test_sh_funds(code):
    assert auto_market(code) == MARKET_SH


def test_sh_b_shares():
    # 900xxx 沪市 B 股 (注意: 920xxx 是北交所, 不能被 9 开头规则吞掉)
    assert auto_market("900901") == MARKET_SH


@pytest.mark.parametrize("code", ["430047", "832566", "836819", "871981", "920002", "920108"])
def test_bj_stocks(code):
    assert auto_market(code) == MARKET_BJ


@pytest.mark.parametrize("code", ["000001", "000858"])
def test_sz_stocks(code):
    assert auto_market(code) == MARKET_SZ


@pytest.mark.parametrize("code", ["300750", "301236"])
def test_sz_chinext(code):
    assert auto_market(code) == MARKET_SZ


@pytest.mark.parametrize("code", ["159915", "127012"])
def test_sz_fund_and_bond(code):
    assert auto_market(code) == MARKET_SZ
