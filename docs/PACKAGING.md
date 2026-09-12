# Windows 构建与发布

## 构建要求

- Python 3.13、Node.js / npm、Inno Setup 6。
- 完整运行依赖与离线模型。首次准备模型需要联网和足够磁盘空间。
- 保持 PyInstaller onedir 和 Inno `DiskSpanning=yes`：轻量安装 EXE 与全部外置 BIN 必须同目录交付，不删模型以缩小包体积。

```powershell
.\build_windows.ps1 -SkipLocalInstall
```

脚本运行 Python／前端测试和类型检查，保存实际源码、模型清单及产物哈希，并用隔离数据库运行打包后的 PDF、OCR 和界面验收。已有完整构建依赖时可额外使用 `-ReuseDependencies`。

**脚本不加 `-SkipLocalInstall` 时会在构建后安装本机。** 原位安装会先检查运行进程、做 SQLite 一致性备份，再核对正式安装路径、构建身份及安装态验收结果。不要强杀运行任务、覆盖用户数据库或删除旧备份。

新构建使用独立时间戳目录，不覆盖旧包。签名可通过构建机环境配置，但证书和密码不能进入仓库。未签名构建应明确告知使用者。

## 公开发布检查

1. 运行 `python tools/check_public_source.py` 并人工审阅待发布文件。
2. 不发布用户数据库、教材、题库、日志、个人路径、密钥、内部源码快照或包含旧私密历史的 Git 对象。
3. 发布二进制前核查全部依赖和模型的许可、版权通知及适用的源码提供义务，参见 `THIRD_PARTY_NOTICES.md`。当前 GitHub 仓库仅发布源码，不自动上传本机旧安装包。
4. EXE 与 BIN 配套发布并附 SHA-256；只上传小 EXE 无法安装。
5. 使用隔离数据验证新安装包，禁止验收任务消耗真实 API、恢复真实业务队列或改写用户教材索引。
