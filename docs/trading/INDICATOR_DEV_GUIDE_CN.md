# QuantDinger 指标开发指南

> 适用对象：QuantDinger 的图表指标开发约定，使用 Pine Script v5/v6 编写
> 读者：初次编写指标的作者、从 Python 指标或 TradingView 脚本迁移过来的开发者，以及审查 AI 生成代码的人

QuantDinger 的图表指标是一段 Pine Script study。Indicator IDE 在用户浏览器里通过 PineTS（用 TypeScript 实现的 Pine 运行时）执行源码，源码原样存储在指标记录的 <code>qd_indicator_codes.code</code> 字段。图表上画出来的东西就是浏览器执行出来的东西：图表指标的任何数值都不在服务端计算。

最重要的边界是：**指标只用于图表分析，不做交易执行。**

指标不能下单、不能回测、不能跑实盘交易、不能读账户、不能管理持仓、不能设置杠杆，也不能执行止损止盈规则。要把一个想法变成交易，需要把它的可视化信号转换成 Strategy API V2 策略，再单独验证、回测和部署那个策略。

因为源码是 Pine，所以语言语义就是 Pine 的语义：<code>ta.*</code> 计算、<code>[1]</code> 历史引用、<code>var</code> 跨 bar 状态、<code>request.security()</code>、<code>input.*</code> 声明，在浏览器运行时实现的子集内，行为与 TradingView 一致。指标没有的东西是账户、订单和持仓。

支持的版本是 **5 和 6**。版本指令必须是文件的第一行：

~~~pine
//@version=6
~~~

低于版本 5 的脚本在到达图表之前就会被拒绝。不能编译的脚本在保存时被拒绝：<code>POST /api/indicator/saveIndicator</code> 和 Agent Gateway 的保存路径会返回 HTTP 400，内容为 <code>Invalid Pine Script: &lt;reason&gt;</code>。保存时的校验由服务端的 Pine 编译器执行（<code>backend_api_python/app/services/pine_validation.py</code>，基于 <code>pynescript</code> 解析器），所以语法错误会在你能修改的地方被指出，而不是等它进入图表之后才暴露。

---

## 1. 先完成一个最小指标

把下面这段贴进 Indicator IDE 并运行：

~~~pine
//@version=6
indicator("Close Line", overlay=true)

plot(close, color=#3B82F6)
~~~

这就是完整的最小约定：

1. 在第一行声明 Pine 版本。
2. 用 <code>indicator("Close Line", overlay=true)</code> 声明 study。标题会成为保存后的指标名，<code>overlay=true</code> 让它画在价格图上而不是独立副图。
3. 用 <code>plot()</code> 绘制。没有 output 字典，也没有返回值：绘制调用本身就是输出。

新建指标时，先加一条线，再加它的 <code>input.*</code> 设置，然后加事件标记，最后才考虑 label、box 这类绘图对象。

---

## 2. 指标、策略和转换流程的边界

| 产物 | 负责 | 不负责 |
| --- | --- | --- |
| 图表指标（Pine study） | plots、fill、标记、label、box、告警条件 | 回测、实盘交易、订单、持仓、杠杆、执行风险 |
| Strategy API V2 | 订阅、信号、订单意图、持仓、回测、实盘执行、保护单 | 图表渲染 |
| 指标转策略 | 把可视化信号的含义翻译成可执行代码 | 把下单行为混进源指标 |

标记只是一次绘制，不是一笔交易。一个标题为“Death”的向下标记，可能表示多头离场警告，也可能只表示条件走弱；它不会开空，也不会反手。

对于只做多的转换，通常的对应关系是：

- 明确的看多入场事件 → <code>open_long</code>
- 明确的看空离场事件 → <code>close_long</code>
- 只有在用户明确要求做空、并给出独立的看空入场规则时，才生成 <code>open_short</code>

不要在指标里制造 <code>open_long</code>、<code>close_long</code>、<code>open_short</code>、<code>close_short</code>、<code>add_long</code>、<code>reduce_long</code> 这类执行列：它们属于 Strategy API V2。也没有 <code># @strategy</code> 注解要写——指标根本没有这一层注解。

<code>strategy(...)</code> 声明在 IDE 里是合法的，图表也仍然会画它的 <code>plot()</code> 输出，但浏览器里什么都不执行：策略的入场和出场只在同一份源码被服务端回测时才发生。保存路径会把这种情况报成 <code>SCRIPT_IS_A_STRATEGY</code> 提示，它是说明而不是错误。

---

## 3. 运行环境：脚本在哪里运行、能读什么

IDE 在浏览器里执行脚本。服务端的职责是在保存时编译和校验源码，并单独运行脚本以服务两类无法使用浏览器的调用方。

| 路径 | 运行位置 | 用途 |
| --- | --- | --- |
| 图表 study | 浏览器，PineTS | 图表上绘制的一切 |
| 保存/校验 | 服务端，Pine 编译器 | 在源码入库前接受或拒绝 |
| 告警与 API 调用方 | 服务端，转译成 Python | 为通知和非浏览器调用方评估信号 |

浏览器运行时通过 Pine 的内置序列读取 bar 数据：<code>open</code>、<code>high</code>、<code>low</code>、<code>close</code>、<code>volume</code>、<code>hl2</code>、<code>hlc3</code>、<code>ohlc4</code>、<code>time</code> 和 <code>bar_index</code>。这里没有 <code>df</code>，也没有 <code>params</code> 字典：序列就是以 bar 为索引的值，设置就是 <code>input.*</code> 调用返回的变量。

可用命名空间：<code>ta</code>、<code>math</code>、<code>str</code>、<code>color</code>、<code>array</code>、<code>matrix</code>、<code>map</code>、<code>request</code>、<code>input</code>、<code>strategy</code>、<code>syminfo</code>、<code>timeframe</code> 和 <code>chart</code>。随运行时提供的库只有一个：<code>TradingView/ta</code>（任意版本），其中的 <code>requestVolumeDelta</code> 把一根 K 线拆成买入与卖出成交量。不要 import 未随运行时一并提供的库：解析不到的 import 会让整段脚本失败。

同一份源码会被两套执行环境读取，由此得到两条规则：

- 图表是在浏览器里对图表已有的 bar 做的重放。没有订单、账户或组合状态可读。
- 告警使用的服务端路径会先把 Pine 源码转译成 Python 再求值。这解释了为什么告警评估的条件与图表画出来的一致。两条路径并不完全相同：服务端重放会折叠 bar 状态，因此依赖实时 bar 的条件必须用 <code>barstate.isconfirmed</code> 加闸，告警才会与图表一致。

历史引用、<code>var</code> 和 <code>na</code> 遵循 Pine 语义。来自未来的序列不存在；<code>close[1]</code> 是上一根 bar，读到第一根可用 bar 之前会得到 <code>na</code>。

---

## 4. 脚本结构与元数据

完整的 study 只有一种结构：

~~~pine
//@version=6
// Dual EMA Viewer — chart-only study, no order logic.

indicator("Dual EMA Viewer", overlay=true)

// 1. Settings
// 2. Calculation
// 3. Drawing
// 4. Alerts
~~~

编译器与保存路径会强制以下规则：

- 第一行是 <code>//@version=6</code>。
- 只有一处 <code>indicator(...)</code> 或 <code>strategy(...)</code> 声明，写在一行内，位于文件靠前处的行首。
- 声明的第一个字符串参数是 study 标题。该标题就是指标库列表里显示的名字，没有单独的名字块。
- 用户可能调整的每个参数都用 <code>input.*</code> 声明，不要留成散落在代码里的字面量。

没有单独的元数据块。已退役的 Python 约定用一句裸赋值（<code>my_indicator_name = "..."</code>）来做同一件事；迁移后的脚本用声明标题替代它，保存路径也优先读取标题。

项目源码规则要求标识符、注释、input 标题和默认显示标签使用英文。只有在用户明确要求目标语言时才本地化显示文本。

---

## 5. 参数声明：编写 input.* 字段

IDE 通过扫描脚本里的 <code>input.*</code> 声明来生成参数面板。没有需要另外声明的东西，也没有需要注册的东西：源码里存在的 input 就会成为表单字段，它的标题、分组、提示和范围都来自你写的参数。

~~~pine
//@version=6
indicator("EMA Channel", overlay=true)

fastLen = input.int(12, "Fast EMA length", minval=1, maxval=200, step=1, group="Parameters", tooltip="Bars used by the fast EMA. Lower values react sooner and whipsaw more.")
slowLen = input.int(26, "Slow EMA length", minval=2, maxval=400, step=2, group="Parameters", tooltip="Bars used by the slow EMA. Must stay above the fast length.")
bandPct = input.float(1.5, "Channel width (%)", minval=0.1, maxval=10.0, step=0.1, group="Parameters", tooltip="Distance of the channel from the fast EMA, in percent.")
showMarks = input.bool(true, "Show crossover markers", group="Visuals")
lineColor = input.color(#3B82F6, "Line color", group="Visuals")
src = input.source(close, "Price source", group="Visuals")
htf = input.timeframe("1D", "Higher timeframe", group="Visuals")
~~~

每个参数在面板里的效果：

| 参数 | 面板效果 |
| --- | --- |
| 第二个参数 <code>title</code> | 字段标签，同时也是保存参数值所用的键 |
| <code>group</code> | 字段被放入的分区 |
| <code>tooltip</code> | 字段旁边显示的帮助文本 |
| <code>minval</code> / <code>maxval</code> | 数值字段允许的最小值和最大值 |
| <code>options=[...]</code> | 用下拉框代替自由输入 |
| 第一个参数 <code>defval</code> | 面板初始值 |

会变成表单字段的 input 构造器：<code>input.int</code>、<code>input.float</code>、<code>input.bool</code>、<code>input.string</code>、<code>input.color</code>、<code>input.source</code>、<code>input.timeframe</code>、<code>input.symbol</code>、<code>input.session</code>、<code>input.price</code>、<code>input.time</code>、<code>input.text_area</code> 和 <code>input.enum</code>。

规则：

- 用 input 声明的标题作为标签。没有标题的字段不会显示。
- 默认值必须是字面量，或是被赋了字面量的常量。面板在脚本运行之前就从源码里读出它，因此计算得出的默认值（<code>input.int(timeframe.period)</code>）没有可显示的值，字段会报无默认值。
- 不要在代码里为同一个设置再起一个名字。声明赋值给的变量就是脚本使用的值：上面的 <code>fastLen</code> 就是长度本身，不是查表用的键。
- 长度或索引参数必须是 <code>int</code>：<code>ta.ema(close, 12)</code> 可以，<code>ta.ema(close, 12.5)</code> 会被编译器拒绝。
- 参数只控制计算和显示。不要在指标里声明账户、持仓、杠杆、止损或止盈参数，它们属于策略。

下拉与受限取值示例：

~~~pine
mode = input.string("Trend", "Mode", options=["Trend", "Range", "Off"], group="Parameters", tooltip="Which branch of the study to draw.")
~~~

---

## 6. 曲线、蜡烛、填充与颜色

<code>plot()</code> 在图表上绘制一条序列。它的参数是值和可选的图例标题与样式：

~~~pine
//@version=6
indicator("EMA Overlay", overlay=true)

emaFast = ta.ema(close, 12)
emaSlow = ta.ema(close, 26)

plot(emaFast, "EMA Fast", color=#22C55E, linewidth=2)
plot(emaSlow, "EMA Slow", color=#3B82F6, linewidth=2)
~~~

- <code>title</code> 是图例显示的名字。读者需要识别的线都要给标题；辅助序列不加标题，或用 <code>display=display.none</code> 隐藏。
- <code>style</code> 选择绘制形式：<code>plot.style_line</code>、<code>plot.style_linebr</code>（值为 <code>na</code> 处断开）、<code>plot.style_stepline</code>、<code>plot.style_columns</code>、<code>plot.style_histogram</code>、<code>plot.style_circles</code>、<code>plot.style_cross</code>、<code>plot.style_area</code>、<code>plot.style_areabr</code>。
- <code>color</code> 接受十六进制字面量或 <code>color.*</code>。逐 bar 变色是序列表达式：<code>color = close >= open ? #22C55E : #EF4444</code>。
- 断点是 <code>na</code>。写 <code>plot(condition ? value : na)</code>；绝不要用一个哨兵值（例如 <code>0</code>）代表缺失点，否则线会穿过零值，歪曲价格。
- <code>offset</code> 按整根 bar 平移绘制。绘图确实需要位移时才用它，不要把位移塞进数值里。

蜡烛和条形有专门的调用：

~~~pine
plotcandle(open, high, low, close, "Custom candles", color=close >= open ? #22C55E : #EF4444, wickcolor=#9CA3AF, bordercolor=#374151)
~~~

<code>plotbar()</code> 用同样的四个值画 OHLC 条形。

水平线与填充区域：

~~~pine
hline(70, "Overbought", color=color.new(#EF4444, 60))
hline(30, "Oversold", color=color.new(#22C55E, 60))

upper = plot(emaFast * 1.005, "Channel upper", color=#22C55E, display=display.none)
lower = plot(emaFast * 0.995, "Channel lower", color=#22C55E, display=display.none)
fill(upper, lower, color=color.new(#22C55E, 85), title="Channel")
~~~

- <code>hline()</code> 接受常量价格。它是一条水平位，不是序列。
- <code>fill()</code> 接受需要填充的两个 <code>plot()</code> 句柄。该传句柄的地方绝不硬编码数值。
- <code>color.new(color, transparency)</code> 构造半透明颜色；0 是不透明，100 是完全透明。价格后方的云带建议透明度高于 60。
- <code>bgcolor()</code> 和 <code>barcolor()</code> 给整个背景或蜡烛着色，每根 bar 一个颜色。用 <code>na</code> 让某根 bar 保持原样。

~~~pine
bgcolor(emaFast > emaSlow ? color.new(#22C55E, 92) : color.new(#EF4444, 92), title="Regime")
barcolor(close >= open ? #22C55E : #EF4444, title="Candle direction")
~~~

### 填充云带

云带是两条 <code>plot()</code> 句柄加一次 <code>fill()</code>。上下边界在绘制时才排序，因此两条线交叉时云带依然正确：

~~~pine
//@version=6
indicator("Trend envelope", overlay=true)

fastLen = input.int(12, "Fast EMA length", minval=1, maxval=200, group="Parameters")
slowLen = input.int(26, "Slow EMA length", minval=2, maxval=400, group="Parameters")
cloudOpacity = input.int(85, "Cloud transparency", minval=60, maxval=98, group="Visuals")

emaFast = ta.ema(close, fastLen)
emaSlow = ta.ema(close, slowLen)

upper = plot(math.max(emaFast, emaSlow), "Upper edge", color=#22C55E)
lower = plot(math.min(emaFast, emaSlow), "Lower edge", color=#F43F5E)
fill(upper, lower, color=emaFast >= emaSlow ? color.new(#22C55E, cloudOpacity) : color.new(#F43F5E, cloudOpacity), title="Trend cloud")
~~~

均线、布林带和价格通道通常叠加在价格图上（<code>overlay=true</code>）。RSI、MACD 等振荡指标通常使用独立副图（<code>overlay=false</code>）；同一个 study 的所有 plot 共享那个副图，因此不会像多个独立 TradingView 脚本那样分成多个副图。

---

## 7. 标记：单根 bar 的事件

标记应该画在“发生了变化”的那根 bar 上，而不是“条件恰好成立”的每一根 bar 上。Pine 直接提供变化事件：

~~~pine
//@version=6
indicator("EMA Cross Markers", overlay=true)

fastLen = input.int(12, "Fast EMA length", minval=1, maxval=200, group="Parameters")
slowLen = input.int(26, "Slow EMA length", minval=2, maxval=400, group="Parameters")
showMarks = input.bool(true, "Show crossover markers", group="Visuals")

emaFast = ta.ema(close, fastLen)
emaSlow = ta.ema(close, slowLen)

crossUp = ta.crossover(emaFast, emaSlow)
crossDown = ta.crossunder(emaFast, emaSlow)

plotshape(showMarks and crossUp, "Golden cross", shape.triangleup, location.belowbar, #22C55E, size=size.small)
plotshape(showMarks and crossDown, "Death cross", shape.triangledown, location.abovebar, #EF4444, size=size.small)
~~~

为什么这个区分重要：

- <code>ta.crossover(a, b)</code> 只在 <code>a</code> 上穿 <code>b</code> 的那一根 bar 上为真；<code>ta.crossunder</code> 是它的镜像。这才是作者所说的“交叉”事件。
- <code>emaFast &gt; emaSlow</code> 这样的连续布尔条件在整个趋势期间的每根 bar 上都为真。把它当作标记条件，会在所有这些 bar 上都画一个形状；用它构造 <code>alertcondition</code>，会在趋势持续的每根 bar 上触发。这是已发布指标里最常见的编写错误。
- 不带条件的布尔值，例如 <code>plotshape(true, ...)</code>，会标记每一根 bar，几乎从来不是想要的。

标记调用：

| 调用 | 用途 |
| --- | --- |
| <code>plotshape(cond, title, shape.triangleup, location.belowbar, color)</code> | 在 bar 固定一侧画内置形状 |
| <code>plotchar(cond, title, "▲", location.belowbar, color)</code> | 用单个字符代替形状 |
| <code>plotarrow(up, "Delta")</code> | 按幅度大小的上下箭头 |

值得设置的标记参数：<code>title</code>（标记与其告警在列表里显示的名字）、<code>location.belowbar</code> / <code>location.abovebar</code>、<code>size</code>、形状上的短标签 <code>text</code>，以及逐 bar 的 <code>color</code>。

标记仍然只是绘制。绿色三角不是多头持仓，它只是“多头入场条件在这根 bar 上成立”。

---

## 8. 告警与其他周期

<code>alertcondition()</code> 声明一个用户可以挂告警的条件。它不下单，自己也不发送任何东西：

~~~pine
alertcondition(ta.crossover(emaFast, emaSlow), "Golden cross", "EMA fast crossed above slow")
alertcondition(ta.crossunder(emaFast, emaSlow), "Death cross", "EMA fast crossed below slow")
~~~

- 第一个参数是条件，第二个是告警在界面里的标题，第三个是消息文本。
- 在事件条件上声明告警。状态条件会在该状态的每一根 bar 上都通知。
- 图表画标记，告警面板列出条件。通道、投递方式等配置由用户在保存后选择；脚本自己发不出通知。
- 信号告警在服务端评估：存储的 Pine 源码被转译成 Python，并在告警所用标的和周期的 bar 上重放。最后一根已完成 bar 上的标记会在下一根 bar 投递，因此告警不会漂到仍在形成的蜡烛上。

其他标的和周期来自 <code>request.security()</code>。这是读取图表当前未显示的数据的唯一方式：

~~~pine
htfClose = request.security(syminfo.tickerid, "1D", close[1], lookahead=barmerge.lookahead_off)
~~~

- 第二个参数是周期，第三个是在该周期上求值的表达式。
- 在高周期上读 <code>close[1]</code> 读的是已经收线的那根值。在那里读 <code>close</code> 读的是仍在变化的那根 bar，它的值会一直变到该高周期 bar 收线。
- <code>lookahead=barmerge.lookahead_off</code> 是诚实的默认值；<code>lookahead_on</code> 会在值还不存在时就显示它，这是回测无法在实盘中复现的常见原因。
- 高周期读取不会切换图表周期。study 仍然运行在图表的 bar 上。

### 等待收盘的 bar

重绘是作者的责任，对应的工具是 <code>barstate.isconfirmed</code>：

~~~pine
confirmedUp = ta.crossover(emaFast, emaSlow) and barstate.isconfirmed

plotshape(confirmedUp, "Confirmed cross", shape.triangleup, location.belowbar, #22C55E)
alertcondition(confirmedUp, "Confirmed cross", "EMA fast crossed above slow on a closed bar")
~~~

当最右侧的 bar 仍在形成时，<code>barstate.isconfirmed</code> 为假，因此标记和告警条件都会等到该 bar 收线。任何读取实时值的条件——当前 close、高周期读取、成交量数值——如果要驱动告警或对历史作断言，都必须这样加闸。

---

## 9. 线段、标签和方框

大多数 study 只需要 plots 和 markers。当标注是一个对象而不是逐 bar 序列时，才使用 Pine 的绘图对象：支撑线、需求区、某个结构事件上的一次性标签。

~~~pine
// A label on each confirmed crossover, deleted when the next one appears.
var label lastMark = na

if confirmedUp
    if not na(lastMark)
        label.delete(lastMark)
    lastMark := label.new(bar_index, low, "Long", style=label.style_label_up, color=#22C55E, textcolor=#FFFFFF)
~~~

~~~pine
// A horizontal level held for the last 100 bars.
var line support = na

if bar_index == last_bar_index - 100
    support := line.new(bar_index, low, last_bar_index, low, color=#F59E0B, width=1, style=line.style_dashed)
~~~

- <code>var</code> 让一个句柄跨 bar 保持，而不是每根 bar 新建一个对象。没有它，study 会每根 bar 造一个新绘制，直到达到对象上限并把最旧的丢掉。
- 删除被取代的对象：<code>label.delete()</code>、<code>line.delete()</code>、<code>box.delete()</code>。留在图上的过期标记会被读成仍然有效的水平位。
- 脚本可保留的对象数量受声明上限控制：<code>indicator(..., max_labels_count=500, max_lines_count=500, max_boxes_count=500)</code>。
- <code>box.new()</code> 画一个区域：上边、下边、左边界、右边界。<code>bgcolor()</code> 不能替代一个起止于特定 bar 的区域。
- 句柄数组是管理集合的方式：<code>array.new_line()</code> 配合 <code>array.push()</code>，需要裁剪时用 <code>line.delete()</code>。

绘图对象依然只是绘制。标着“Support”的方框不是订单、不是止损、不是持仓。

---

## 10. 从 Python 指标迁移到 Pine

Python 指标约定已退役。存储的指标是 Pine 源码；仍保存 Python 的记录在保存时会被拒绝，因为源码不再能编译。重写是机械性的，下表就是全部内容：

| Python 指标（已退役） | Pine study（当前） |
| --- | --- |
| <code>my_indicator_name = "..."</code> | <code>indicator("...")</code> 里的标题 |
| <code>my_indicator_description = "..."</code> | 存在时仍会作为该记录行的描述被读取；Pine 没有描述语法，否则它来自保存请求 |
| <code># @param fast_len int 12 Fast EMA period</code> | <code>fastLen = input.int(12, "Fast EMA period", minval=1)</code> |
| <code>params.get("fast_len", 12)</code> | 声明产生的变量 <code>fastLen</code> |
| <code>df</code> 工作副本 | 内置序列 <code>open/high/low/close/volume</code> |
| pandas 或 numpy 运算 | <code>ta.*</code>、<code>math.*</code> 和普通序列运算 |
| <code>output</code> 字典 | <code>plot()</code>、<code>plotshape()</code> 以及各类绘图调用 |
| 带 <code>upper</code>/<code>lower</code> 的云带条目 | 两条 <code>plot()</code> 句柄加一次 <code>fill()</code> |
| 带 <code>None</code> 空洞的标记列表 | 单根 bar 事件上的 <code>plotshape(condition, ...)</code> |
| 区域、线段或标签图层对象 | <code>box.new()</code>、<code>line.new()</code>、<code>label.new()</code> |
| 用 <code>None</code> 表示缺失值 | <code>na</code> |

步骤：

1. 把逻辑重写成 Pine。没有 DataFrame 可切，也没有向量化的等价写法：<code>ta.ema(close, len)</code> 替代手写的 <code>ewm</code> 链，<code>close[1]</code> 替代 <code>shift</code>。
2. 删掉 <code># @param</code> 块，改用 <code>input.*</code> 声明，保留同样的默认值，并补上旧代码隐含的范围（<code>minval</code>、<code>maxval</code>、<code>step</code>）。
3. 删掉 <code>output</code> 字典，为每个条目调用对应的绘图函数：plot 条目改为 <code>plot()</code>，marker 条目改为 <code>plotshape()</code>，云带条目改为两条 plot 加一次 <code>fill()</code>。
4. 保留计算，丢掉管道。哨兵值、索引技巧和 <code>df</code> 复制那一步都会消失。
5. 保存。参数面板由代码推导而来，之后没有需要同步的东西：修改一行 <code>input.*</code> 就是在修改表单。

参数面板自我推导，这也意味着用户应该能改的值不能是埋在函数体里的字面量——迁移时就把它提升为 input，否则以后无法调整。

---

## 11. Pine 类型与序列陷阱

若干 Python 习惯翻译过来会出问题。以下这些会让图表看起来合理但实际是错的：

- <code>na</code> 是 Pine 的缺失值，并且会传播。与 <code>na</code> 运算得到 <code>na</code>，与 <code>na</code> 比较既不真也不假，绘制 <code>na</code> 就是断点。除非 study 确实想表达零，否则不要用 <code>nz()</code> 掩盖它：<code>nz(rsi)</code> 会把预热期的空缺变成 0，而 0 是一个真实的 RSI 读数。
- 整数除法会截断。<code>1/2</code> 是 <code>0</code>，不是 <code>0.5</code>；需要比率时写 <code>1.0/2</code>，或使用浮点 input。
- 长度和索引必须是整数。<code>ta.ema(close, len)</code> 要求 <code>int</code>；浮点长度是编译错误，不是四舍五入。
- 历史偏移只能读过去。<code>close[1]</code> 是上一根 bar，读到图表数据的第一根之前是 <code>na</code>。长回看要用 <code>bar_index &gt; n</code> 保护。
- <code>var</code> 和 <code>varip</code> 跨 bar 保持状态。正是它让计数器和绘制句柄能工作，也正是它让脚本的行为不同于每次全新求值：用 <code>na</code> 或字面量初始化，并在该赋值的那根 bar 上赋值。
- 序列不能用在需要简单值的地方，例如 input 默认值或周期字符串。<code>timeframe.period</code> 只读一次，是简单值；<code>str.tostring(close)</code> 是序列。
- 标题或消息里的字符串是字面量。<code>alertcondition()</code> 里的告警消息无法用序列拼接；消息是固定文本。
- 除以零会得到 <code>na</code> 或无穷大。要保护分母（<code>den == 0 ? na : num/den</code>），不要让尖峰画进图里。

---

## 12. 避免未来数据和重绘

study 只能使用当前和更早的 bar。在 Pine 里破坏这一点的常见方式：

- 用 <code>request.security()</code> 读取仍在形成的高周期 bar 并把它当成最终值；
- 在高周期读取上使用 <code>lookahead=barmerge.lookahead_on</code>，在产生该值的 bar 还不存在时就显示它；
- 绘制依赖实时 close 的条件，然后把那个标记描述成历史事实；
- 用 <code>barstate.islast</code> 改写更早的 bar 本应显示的内容；
- 因为信号“本来应该”出现在那里，就把标记挪到更早的 bar。

诚实的交叉读取当前与上一根状态，并把事件闸在已收盘的 bar 上：

~~~pine
confirmedUp = ta.crossover(emaFast, emaSlow) and barstate.isconfirmed
~~~

这会推迟事件到收线之后；它从不读取更晚的 bar。如果信号需要收盘确认，交易它的策略也会更晚执行。不要为了让图表或回测更好看而把信号往前挪。

---

## 13. 语言限制与安全性

Pine study 做不了危险的事，剩下的约束说的是能表达什么，而不是被允许什么：

- 没有文件系统、网络、数据库、进程或时钟访问。一切都来自图表的 bar 和脚本的输入。
- import 只能解析到随运行时提供的来源。解析不到的 import 会让整段脚本失败。
- 每个文件只有一处 <code>indicator(...)</code> 或 <code>strategy(...)</code> 声明。
- 没有账户、订单、持仓或组合状态。<code>strategy.*</code> 调用合法，但在浏览器里没有可作用的对象。
- 编译器就是闸门。不能编译的源码在保存时被 HTTP 400 拒绝，永远不会到达图表。
- 保持逐 bar 计算有界。每根 bar 都在大数组上循环的脚本是正确的，但很慢，预览和告警重放都要为它付出代价。

---

## 14. 完整教程：双 EMA 交叉指标

~~~pine
//@version=6
// Dual EMA Viewer — chart-only study. Visual markers, no order logic.

indicator("Dual EMA Viewer", overlay=true)

// ===== Settings =====
fastLen = input.int(12, "Fast EMA length", minval=5, maxval=30, step=1, group="Parameters", tooltip="Bars used by the fast EMA.")
slowLen = input.int(26, "Slow EMA length", minval=10, maxval=80, step=2, group="Parameters", tooltip="Bars used by the slow EMA.")
showMarks = input.bool(true, "Show crossover markers", group="Visuals")
showLabels = input.bool(false, "Label each crossover", group="Visuals")
fastColor = input.color(#22C55E, "Fast EMA color", group="Visuals")
slowColor = input.color(#3B82F6, "Slow EMA color", group="Visuals")

// ===== Calculation =====
emaFast = ta.ema(close, fastLen)
emaSlow = ta.ema(close, slowLen)

// A crossover is a one-bar event. A continuous condition would mark every bar
// of the trend, and an alert on it would fire on every one of those bars.
golden = ta.crossover(emaFast, emaSlow)
death = ta.crossunder(emaFast, emaSlow)

// Only count the event once the bar has closed, so the study does not repaint.
confirmedGolden = golden and barstate.isconfirmed
confirmedDeath = death and barstate.isconfirmed

// ===== Drawing =====
plot(emaFast, "EMA Fast", color=fastColor, linewidth=2)
plot(emaSlow, "EMA Slow", color=slowColor, linewidth=2)

plotshape(showMarks and confirmedGolden, "Long Entry", shape.triangleup, location.belowbar, #22C55E, size=size.small, text="L")
plotshape(showMarks and confirmedDeath, "Long Exit", shape.triangledown, location.abovebar, #EF4444, size=size.small, text="X")

var label lastLabel = na

if showLabels and confirmedGolden
    if not na(lastLabel)
        label.delete(lastLabel)
    lastLabel := label.new(bar_index, low, "Golden cross", style=label.style_label_up, color=#22C55E, textcolor=#FFFFFF)

// ===== Alerts =====
alertcondition(confirmedGolden, "Golden cross", "EMA fast crossed above slow on a closed bar")
alertcondition(confirmedDeath, "Death cross", "EMA fast crossed below slow on a closed bar")
~~~

它是怎么工作的：

1. 声明给出 study 名字，并说明它叠加在价格上绘制。
2. 每个可调值都是 <code>input.*</code>，所以设置面板会由源码自行构建，带上这里写的范围和帮助文本。
3. EMA 是持久序列，所以用 plot 绘制。
4. 交叉是单根 bar 事件，所以用标记，并且两者都闸在 <code>barstate.isconfirmed</code> 上。
5. 标记命名为 “Long Entry” 和 “Long Exit”。看空标记是离场，不是自动开空——那个决定属于转换，不属于绘制。
6. 告警条件是声明。它们负责通知，不负责交易。
7. 脚本里任何地方都没有订单、持仓或回测。

把同一个 study 打开在另一个标的和周期上：代码没有指定其中任何一个，因为图表提供它们。

---

## 15. 验证、调试和常见错误

每次有意义的修改之后按这个流程走：

1. 在 Indicator IDE 里粘贴或修改源码。
2. 保存。服务端编译源码；语法错误会以 HTTP 400 <code>Invalid Pine Script: &lt;reason&gt;</code> 返回，并带上出错行。
3. 在图表上打开 study，检查绘制结果。
4. 改一个输入，确认绘制随之变化。
5. 确认标记只出现在事件 bar 上，而不是趋势的每一根 bar 上。
6. 挂一个告警条件，确认它在你预期的 bar 上触发。

IDE 在编辑过程中还会报告非阻断的质量提示。它们描述结构，不描述数值：

| 提示 | 级别 | 含义 | 如何消除 |
| --- | --- | --- | --- |
| <code>EMPTY_CODE</code> | error | 源码为空 | 提供完整脚本 |
| <code>MISSING_PINE_VERSION</code> | warn | 缺少 <code>//@version=</code> 行 | 把 <code>//@version=6</code> 放在第一行 |
| <code>UNSUPPORTED_PINE_VERSION</code> | error | 声明的版本低于 5 | 把源码改写成版本 5 或更高 |
| <code>UNKNOWN_PINE_VERSION</code> | warn | 声明的版本高于 6 | 以 6 为目标，那是运行时实现的版本 |
| <code>PINE_SYNTAX_ERROR</code> | error | 解析器拒绝了源码 | 修正报告的行；消息里带位置 |
| <code>MISSING_SCRIPT_DECLARATION</code> | warn | 没有 <code>indicator(...)</code> 或 <code>strategy(...)</code> 调用 | 在独立一行加上恰好一处声明 |
| <code>MISSING_SCRIPT_TITLE</code> | info | 声明没有标题 | 把 study 名字作为第一个参数传入 |
| <code>SCRIPT_IS_A_STRATEGY</code> | info | 源码声明了 <code>strategy(...)</code> | 对策略而言是预期的；图表 study 应声明 <code>indicator(...)</code> |
| <code>NO_PLOTS_DECLARED</code> | warn | 脚本什么都没画 | 加上 <code>plot</code>、<code>plotshape</code>、<code>fill</code>、<code>hline</code>、<code>bgcolor</code> 或 <code>barcolor</code> 调用 |

级别为 <code>warn</code> 和 <code>info</code> 的提示不阻断保存。级别为 <code>error</code> 的会：源码不会被存储，响应里带回原因。

不属于提示的常见故障：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 每根 bar 都有标记 | 条件描述的是状态，不是事件 | 使用 <code>ta.crossover</code> / <code>ta.crossunder</code> |
| 每根 bar 都告警 | 同样是把状态当条件 | 把告警建在单根 bar 事件上 |
| “找不到声明” | 版本行不在第一行，或声明被缩进、被换行 | 第一行、行首、一行内 |
| 面板里缺少字段 | input 没有标题 | 给 input 加标题 |
| 字段没有默认值 | 默认值是表达式而不是字面量 | 使用字面量默认值 |
| 线穿过零值 | 把断点画成了 <code>0</code> | 断点绘制 <code>na</code> |
| 最后一根 bar 上数值会变 | 脚本读取了实时值 | 用 <code>barstate.isconfirmed</code> 加闸 |

---

## 16. 转换成策略前的语义清单

转换之前先回答这些问题：

- 哪个标记是多头入场？
- 哪个标记是多头离场？
- 是否真的需要做空？看空离场并不自动等于开空入场。
- 是否需要反手？如果需要，平仓和反向开仓是两个独立动作吗？
- 信号在哪个周期、哪根已完成的 bar 上确认？
- 是否允许重复入场、分批建仓或分批减仓？
- 仓位、止损、止盈和移动保护应该如何工作？
- 策略将拥有哪个标的、哪种市场类型？
- 如果是交易所股票，执行到底使用哪个目录产品、场所、币种、产品类型和 API 家族？保留 <code>Crypto:00700/HKD@gate:spot</code> 这样的标的，或经目录确认的 <code>Crypto:HK0700/USDT@binance:swap</code>，不要用它的底层股票替换它（API family 是产品契约里的标识符，翻译后无法与代码对上）。
- 该产品是只做多的现货股票，还是需要显式方向元数据和 <code>position_side</code> 的 Crypto 永续合约？

转换后的策略会丢掉纯图表的部分：颜色、plot 样式、offset、标记、label 和 fill。保留信号代数——条件以及它们触发的那根 bar——并用 Strategy API V2 显式声明标的、频率、仓位和风险。如果原 study 本身已经用了 <code>strategy(...)</code> 并且条件写成了策略入场，那么转换主要是把那份源码搬到策略侧。

底层股票数据只有在活动目录提供有效 <code>USStock</code> 或 <code>HKStock</code> 映射时，才能支撑图表历史和点时基本面。该数据映射从不改变执行标的，转换器也不得根据显示名去猜区域。

生成的策略代码始终要重新验证和回测。上架市场要求至少一条成功的回测记录。

---

## 17. 发布前检查清单

- [ ] 第一行是 <code>//@version=6</code>（若目标是 v5，则为 <code>//@version=5</code>）。
- [ ] 只有一处 <code>indicator(...)</code> 声明，标题能说明这个 study 是什么。
- [ ] study 有绘制内容：一条线、一个标记、一次填充或一条水平位。
- [ ] 每个可调值都是带标题的 <code>input.*</code>，数值 input 带有合理的 <code>minval</code>/<code>maxval</code>/<code>step</code>。
- [ ] 注释、标识符、input 标题和 plot 标题使用英文。
- [ ] 标记是基于 <code>ta.crossover</code> / <code>ta.crossunder</code> 的单根 bar 事件，不是持久状态。
- [ ] 任何读取实时值的逻辑都用 <code>barstate.isconfirmed</code> 加闸。
- [ ] 高周期读取使用 <code>request.security()</code> 且不带 <code>lookahead_on</code>。
- [ ] 没有任何订单、持仓、杠杆或执行风险逻辑。
- [ ] 缺失值用 <code>na</code>；没有用 <code>0</code> 之类的哨兵值代替断点。
- [ ] 没有读取未来数据：没有实时高周期值，没有改写过去的 bar。
- [ ] 短数据集、预热期和异常参数不会让预览崩溃。
- [ ] 脚本保存时没有 <code>error</code> 级别的提示，图表预览画出的内容与描述一致。
