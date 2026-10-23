# 海纳知识库 · 核心代码副本

整理自当前项目，仅保留运行源码。原项目没有改动。

- `core/`：领域逻辑、用例、端口、适配器和数据库结构迁移。
- `apps/`：Web 后端、原生前端、本地前端库及 worker。
- `tools/auth/`、`tools/reindex/`、`tools/pg/migrate_kb.py`：账号管理、索引重建和数据迁移命令。
- `pyproject.toml`：Python 版本及运行依赖；已移除测试依赖和检查配置。

不含业务数据、原件、测试、测试夹具、评测与探测工具、日志、报告、缓存、虚拟环境、`.env` 或密钥。
`core` 中仅用于模型评测的两个 `model_eval.py` 也未纳入。
前端 vendor 文件是运行依赖，其来源和许可证说明保留在 `apps/web/static/vendor/README.md`。

原 Web 从测试工具导入的常量和两个命名函数，已原样抽取到 `apps/runtime.py`；
仅调整本副本 `assembly.py`、`jobs.py`、`services.py` 的对应导入，业务逻辑不变。
其中 `ANSWER_FILE` 只是禁止上传文件的名称常量，并未携带该文件。

需要 Python 3.11+。运行依赖见 `pyproject.toml` 的基础与 web 项。
本目录不是已配置的数据实例，运行前需另行配置服务环境变量和数据存储。
可先查看命令帮助：

```powershell
python -m apps.web --help
python -m apps.worker --help
python -m tools.auth --help
python -m tools.reindex --help
python -m tools.pg.migrate_kb --help
```
