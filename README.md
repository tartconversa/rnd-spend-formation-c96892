# 园区研发项目的支出形成仓

合成业务资料的入仓、事实建模与集市查询。字段、单位和业务记录的含义见
[docs/资料口径.md](docs/资料口径.md)，仓表如何落实这些口径见
[docs/仓表口径.md](docs/仓表口径.md)。规则只用于内部管理，不生成正式
会计凭证或税务申报；不得把文件自检成功解释成业务结论成立。

## 环境

Python 3.11.2；依赖已在 requirements.txt 固定（DuckDB）。

    python3 -m pip install -r requirements.txt

## 资料自检（不解释业务）

    python3 -m sourcepack
    python3 -m unittest discover -s tests -v
    python3 -m compileall -q sourcepack rdwarehouse tests

## 数据仓使用

DuckDB 文件默认 `warehouse.duckdb`（已在 .gitignore），也可用 `--db :memory:`。

    # 1. 按接收批次装载资料目录（同 sha256 文件自动跳过，不增加事实）
    python3 -m rdwarehouse --db warehouse.duckdb load --data data --note 基础批次

    # 2. 按最新批次重算全部事实（可重复执行，幂等）
    python3 -m rdwarehouse --db warehouse.duckdb build

    # 3. 三种管理视角（口径各自独立，不共用金额列）
    python3 -m rdwarehouse --db warehouse.duckdb query --view budget     --period 2026-10   # 预算使用：承诺+待核
    python3 -m rdwarehouse --db warehouse.duckdb query --view expense    --period 2026-11   # 当期费用：验收费用+资产分担
    python3 -m rdwarehouse --db warehouse.duckdb query --view cip        --period 2026-12   # 在建资产：归集与分担进度
    # 采购行下钻（承诺→收货/验收/票/款/资产去向）
    python3 -m rdwarehouse --db warehouse.duckdb query --view line       --line L1 --period 2026-10
    # 预算占用反查：未履约 / 已形成未结算 / 现金已付为何不同（含桥接校验列）
    python3 -m rdwarehouse --db warehouse.duckdb query --view occupation --period 2026-10
    # 组织视角：默认按业务月当时组织；--current-date 切到查询日当前组织
    python3 -m rdwarehouse --db warehouse.duckdb query --view monthly --period 2026-11 --current-date 2026-12-01
    # 例外/待核/未决差额清单
    python3 -m rdwarehouse --db warehouse.duckdb query --view exceptions

    # 4. 月度管理包：草稿 → 确认（确认后同一 月份+批次 不可改写）→ 跨批次比较
    python3 -m rdwarehouse --db warehouse.duckdb package build   --period 2026-10
    python3 -m rdwarehouse --db warehouse.duckdb package confirm --period 2026-10
    python3 -m rdwarehouse --db warehouse.duckdb package list
    python3 -m rdwarehouse --db warehouse.duckdb package compare --period 2026-10 --base-batch 1 --new-batch 2

### 后续批次怎么装

把新接收的同格式 CSV 放进一个**新目录**（可只含变化的表），再次 `load`
即形成批次 2、3……；同一记录键的新文件是整行修订，raw 层保留全部历史版本，
事实层按最新视野重算，已确认的管理包原样保留，再用 `package compare`
比较补到资料后的变化。

## 模块结构

| 模块 | 职责 |
| --- | --- |
| `rdwarehouse/contract.py` | 输入资料字段契约（与 catalog.json 对齐） |
| `rdwarehouse/loader.py` | 批次装载、文件指纹去重、整行修订视野 |
| `rdwarehouse/schema.py` | DuckDB 仓表 DDL（raw/fact/pkg 三层） |
| `rdwarehouse/fx.py` | 业务当天汇率查找，缺日不插值，CNY=1 |
| `rdwarehouse/money.py` | Decimal 金额、到分舍入与尾差规则、待分残量 |
| `rdwarehouse/transform.py` | 事实重算引擎：承诺/待核/形成/票款/资产/分担/例外 |
| `rdwarehouse/org.py` | 项目-组织时态解析（历史视角/当前视角） |
| `rdwarehouse/package.py` | 月度管理包冻结、确认、跨批次比较 |
| `rdwarehouse/query.py` | 三视角集市、行级下钻、预算占用反查 |
| `sourcepack/` | 原有资料容器检查器，不解释业务 |

## 测试

`tests/test_money.py` 覆盖金额舍入/尾差/待分残量；
`tests/test_warehouse.py` 覆盖三口径分离、RV1 修订冲突、退货关联、
红冲/退款/退货独立、缺汇率待核、资产启用前归集与后补重算、
usage 覆盖不足留待分、出资比例不代替 usage、组织时态、
同摘要去重、整行修订历史、管理包不可变与跨批次比较。
