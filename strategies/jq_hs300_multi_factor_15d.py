# 克隆自聚宽文章：https://www.joinquant.com/post/1399
# 标题：【量化课堂】多因子策略入门
# 作者：JoinQuant量化课堂
#
# 与本地 AkShare 对齐：strategies/hs300_multi_factor_core.py 复刻了 fillNan / getRank / bubble 算法
# 本地运行：python strategies/run_hs300_akshare.py --screen --date YYYY-MM-DD

# 多因子策略入门 — 沪深300，15 日调仓，持 3 只


def initialize(context):
    set_params()
    set_variables()
    set_backtest()


def set_params():
    g.tc = 15
    g.yb = 63
    g.N = 3
    g.factors = ["market_cap", "roe"]
    g.weights = [[1], [-1]]


def set_variables():
    g.t = 0
    g.if_trade = False


def set_backtest():
    set_option("use_real_price", True)
    log.set_level("order", "error")


def before_trading_start(context):
    if g.t % g.tc == 0:
        g.if_trade = True
        set_slip_fee(context)
        g.all_stocks = set_feasible_stocks(get_index_stocks("000300.XSHG"), g.yb, context)
        g.q = query(valuation, balance, cash_flow, income, indicator).filter(
            valuation.code.in_(g.all_stocks)
        )
    g.t += 1


def set_feasible_stocks(stock_list, days, context):
    suspened_info_df = get_price(
        list(stock_list),
        start_date=context.current_dt,
        end_date=context.current_dt,
        frequency="daily",
        fields="paused",
    )["paused"].T
    unsuspened_index = suspened_info_df.iloc[:, 0] < 1
    unsuspened_stocks = suspened_info_df[unsuspened_index].index
    feasible_stocks = []
    for stock in unsuspened_stocks:
        if sum(attribute_history(stock, days, unit="1d", fields=("paused"), skip_paused=False))[0] == 0:
            feasible_stocks.append(stock)
    return feasible_stocks


def set_slip_fee(context):
    set_slippage(FixedSlippage(0))
    dt = context.current_dt
    if dt > datetime.datetime(2013, 1, 1):
        set_commission(PerTrade(buy_cost=0.0003, sell_cost=0.0013, min_cost=5))
    elif dt > datetime.datetime(2011, 1, 1):
        set_commission(PerTrade(buy_cost=0.001, sell_cost=0.002, min_cost=5))
    elif dt > datetime.datetime(2009, 1, 1):
        set_commission(PerTrade(buy_cost=0.002, sell_cost=0.003, min_cost=5))
    else:
        set_commission(PerTrade(buy_cost=0.003, sell_cost=0.004, min_cost=5))


def handle_data(context, data):
    if g.if_trade:
        g.everyStock = context.portfolio.portfolio_value / g.N
        todayStr = str(context.current_dt)[0:10]
        a, b = getRankedFactors(g.factors, todayStr)
        points = np.dot(a, g.weights)
        stock_sort = b[:]
        points, stock_sort = bubble(points, stock_sort)
        toBuy = stock_sort[0 : g.N].values
        order_stock_sell(context, data, toBuy)
        order_stock_buy(context, data, toBuy)
    g.if_trade = False


def order_stock_sell(context, data, toBuy):
    list_position = context.portfolio.positions.keys()
    for stock in list_position:
        if stock not in toBuy:
            order_target(stock, 0)


def order_stock_buy(context, data, toBuy):
    for i in range(0, len(g.all_stocks)):
        if indexOf(g.all_stocks[i], toBuy) > -1:
            order_target_value(g.all_stocks[i], g.everyStock)


def indexOf(e, a):
    for i in range(0, len(a)):
        if e == a[i]:
            return i
    return -1


def getRankedFactors(f, d):
    df = get_fundamentals(g.q, d)
    res = [([0] * len(f)) for i in range(len(df))]
    for i in range(0, len(df)):
        for j in range(0, len(f)):
            res[i][j] = df[f[j]][i]
    fillNan(res)
    getRank(res)
    return res, df["code"]


def getRank(r):
    indexes = list(range(0, len(r)))
    for k in range(len(r[0])):
        for i in range(len(r)):
            for j in range(i):
                if r[j][k] < r[i][k]:
                    indexes[j], indexes[i] = indexes[i], indexes[j]
                    for l in range(len(r[0])):
                        r[j][l], r[i][l] = r[i][l], r[j][l]
        for i in range(len(r)):
            r[i][k] = i + 1
        for i in range(len(r)):
            for j in range(i):
                if indexes[j] > indexes[i]:
                    indexes[j], indexes[i] = indexes[i], indexes[j]
                    for k2 in range(len(r[0])):
                        r[j][k2], r[i][k2] = r[i][k2], r[j][k2]
    return r


def fillNan(m):
    rows = len(m)
    columns = len(m[0])
    for j in range(0, columns):
        sumv = 0.0
        count = 0.0
        for i in range(0, rows):
            if not (isnan(m[i][j])):
                sumv += m[i][j]
                count += 1
        avg = sumv / max(count, 1)
        for i in range(0, rows):
            if isnan(m[i][j]):
                m[i][j] = avg
    return m


def bubble(numbers, indexes):
    for i in range(len(numbers)):
        for j in range(i):
            if numbers[j][0] < numbers[i][0]:
                numbers[j][0], numbers[i][0] = numbers[i][0], numbers[j][0]
                indexes[j], indexes[i] = indexes[i], indexes[j]
    return numbers, indexes


def after_trading_end(context):
    return
