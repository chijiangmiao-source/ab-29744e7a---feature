# 星载启动参数槽 · 扇区镜像恢复审查

地面审查工具：从星载控制器导出的扇区镜像（Base64 定长扇区，按**物理写入顺序**排列）
逐字节裁决「断电后实际会从哪一代完整参数启动」，绝不能把写到一半的较新槽误报为可启动。

## 裁决原则（与需求逐条对应）

- 每条扇区原始 **64 字节**（Base64 后恰为 88 字符），至多 32 条；魔数、版本、类型、
  保留字段、头部校验、**整扇区 CRC32**、槽页的**载荷 CRC32** 全部逐字节校验。
- 扇区只可能是三类：`1=槽页 / 2=准备记录 / 3=完成记录`。
- 仅当同一事务的 **准备 → 目标槽完整页 → 完成** 三段齐备，且事务标识、代次、槽名、
  载荷摘要相符、物理写入顺序正确时，才切换该槽。
- 以下情形均定位到**首个违约扇区下标**并在页面/API 中给出代码与说明：
  Base64 截断、定长不符、魔数/版本/类型错误、CRC32 错误、保留区非零、
  重复事务标识但内容不一致、完成记录指向不存在事务、完成记录缺少对应完整页、
  槽页先于匹配的准备记录、槽页摘要不符、完成记录与准备字段不符。
- **绝不**用最新页号裁决、**绝不**忽略损坏记录、**绝不**只看完成标记。
- 恢复时保留每个槽**最后一个有效代次**的内容与摘要；其后出现的无效/更旧写入
  不得覆盖它（更旧代次的完整事务也会被明确舍弃，防回滚）。
- 代次最高者启动；代次相同则按**物理槽名稳定（字典序）**选择，结论与初始活动槽无关。
- 每条记录在页面上标注「采纳/舍弃」及具体依据；提交按**稳定审计标识冻结**，
  之后只能按标识重新查看冻结结论，重复提交返回 409。

扇区线格式详见 `app/parser.py` 顶部注释；构造/损坏工具见 `app/builders.py`。

## 运行（Docker Compose）

```bash
docker compose up web --build
# 宿主机端口可配置：
HOST_PORT=9090 docker compose up web --build
# 健康检查：
curl -s http://localhost:8080/healthz
```

浏览器打开 `http://localhost:8080/`：填写稳定审计标识、初始活动槽，粘贴每行一个的
Base64 扇区后提交；或输入已冻结标识点击「按标识查看冻结结论」。

冻结结论持久化在 compose 卷 `audit-data`（容器内 `/data/audits.json`）。

## 一次性 verify 服务

`verify` 服务针对**完整切换、完成标记损坏、旧有效槽保留**执行：
构建检查（compileall）→ 全部代码测试（unittest）→ 对运行中 web 容器的 HTTP 冒烟，
运行一次后以退出码报告结果（成功 0，失败非 0）：

```bash
docker compose up --build verify
# docker compose run 也可，退出码同样透传：
docker compose build && docker compose run --rm verify; echo "exit=$?"
```

## 本地无 Docker 时

纯 Python 3.11 标准库，无第三方依赖：

```bash
python3 -m unittest discover -s tests          # 代码测试
python3 -m data.make_samples                   # 重新生成 data/*.txt 样例
DATA_DIR=./_data PORT=8080 python3 -m app.serve
BASE_URL=http://127.0.0.1:8080 python3 tests/verify.py
```

## 目录

| 路径 | 说明 |
| --- | --- |
| `app/parser.py` | 逐字节解析、CRC32、事务状态机与启动代次裁决 |
| `app/builders.py` | 64 字节扇区构造/截断/损坏工具（测试与样例使用） |
| `app/storage.py` | 按审计标识一次写入、fcntl 加锁的冻结结论存储 |
| `app/api.py` / `app/page.py` | HTTP API（healthz / 提交 / 冻结回看）与真实 API 驱动页面 |
| `app/serve.py` | 服务入口（HOST/PORT/DATA_DIR 可配置） |
| `tests/test_recovery.py` | 21 个解析/裁决/保留/存储用例 |
| `tests/test_http.py` | 6 个真实 socket HTTP 冒烟用例 |
| `tests/verify.py` | compose verify 服务的一次性检查脚本 |
| `data/0[1-3]_*.txt` | 三组样例镜像：完整切换 / 完成标记损坏 / 旧有效槽保留 |
