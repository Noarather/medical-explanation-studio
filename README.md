# 医学题库智能解析

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

自有源码采用 [AGPL-3.0-only](LICENSE)，第三方组件保留各自许可，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。

这是一个独立的 Windows 桌面程序（当前版本 2.1.3，Vue 3 + Qt WebEngine）。它为用户添加的医学教材 PDF 建立检索库，为 JSON/Excel 题目生成带教材证据的解析，支持自动校验与异常人工复核，再导出标准 JSON/XLSX 或增量文件。程序不会连接或修改其他题库网站。

## 2.1.3 更新

- 教材可批量导入，尝试自动识别名称、学科和版本，并自动校准印刷页码；识别结果需人工核对，导入和页码校准不等于已建立索引。
- 修复中文 PDF 路径解析、教材多选弹窗滚动和索引完成后的状态刷新。
- 索引按批保存解析结果及向量，失败重试复用匹配的断点；完成校验后才替换旧索引。
- 解析后释放模型；原生内存无法回收时，桌面工作进程受控重启并续跑。同一断点重启后仍超限则停止，避免无进展循环。直接使用 CLI 扫描时可重新执行以复用断点。
- 已落盘的向量批次不会重复请求；云端成功但尚未落盘就中断的最后一批仍可能重复收费。

## 功能

- 单本 PDF 教材、学科与版本管理，按 SHA-256 增量更新。
- 支持用“PDF 页 ↔ 课本印刷页”锚点校准页码；解析引用实际课本页，PDF 原页仍准确定位。
- 离线 Docling 解析布局、表格、公式及中文 OCR；PyMuPDF／RapidOCR 故障回退，本地失败时才使用配置的 Qwen OCR。旧索引逐本确认重建，重建前备份。
- SQLite FTS5 BM25 + embedding 候选检索，DashScope Rerank 排序；失败回退至 embedding，综合门槛须经过黄金集标定。
- 支持 DeepSeek、Claude、Gemini 及自定义 API 生成解析、标签、考点和知识卡；设置页提供模型测试与版本／构建标识。
- JSON/Excel 导入、字段映射、重复 ID 和必填字段校验。
- 多文件批量导入、来源模板、精确去重和冲突拦截；后台进度与取消、异常分页和字段表单，保留高级 JSON 编辑。入库提交阶段不可取消。
- 兼容按章节 JSON：自动展开 `[{章节, 题目:[...]}]`，识别 `ID/题型/题干/选项/答案/解析` 中文字段，并由文件名推断学科。
- 原始题目文本智能整理：支持粘贴或读取 TXT、Markdown、DOCX，本地识别失败时可调用 DeepSeek，预览补全后再导入。
- 智能整理结果按“待调整 / 可导入”分组；支持先导入全部有效题，再批量设置剩余题目的学科、题型、章节或难度。
- 独立后台任务，支持暂停、继续、取消、失败重试和重启恢复。
- 三栏人工审核，可编辑解析、查看证据片段和教材 PDF 原页。
- 人工审核题目列表支持 Ctrl/Shift 多选、本页全选、批量设置学科及仅重新生成所选题目。
- 导出完整题目 JSON、精简解析映射和问题报告。
- 复习考点补齐：为已生成题目批量生成 1～3 条考点（标题+复习正文），UI「补齐考点」按钮或 CLI `study-points`。
- 生成内容可选：生成时可勾选解析/标签/考点（CLI `--only`），导出支持按学科拆分（UI 复选框 / CLI `--split-by-subject`）。

## 首次运行

```powershell
git clone https://github.com/Noarather/medical-explanation-studio.git
cd medical-explanation-studio
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cd frontend
npm ci
npm run build
cd ..
python tools/prefetch_docling_models.py
python desktop_app.py
```

需要 Python 3.13、Node.js / npm 和 Windows。模型准备需要联网并下载较大文件，完成后教材主解析和中文 OCR 可离线运行。使用云端向量、重排或生成服务需自行配置账户和密钥，并承担对应服务费用。

也可以右键使用 PowerShell 运行 `run_desktop.ps1`。启动后在“设置”页录入 DeepSeek 和 DashScope 密钥。密钥由 Windows 凭据管理器保存，不写入配置文件或数据库。

> 如果旧配置文件中曾保存过真实密钥，请先在对应平台撤销并重新生成密钥。

## 使用顺序

1. 在“教材库”点击“添加 PDF 教材”，选择一个 PDF 并填写名称、学科和版本。若两套页码不同，在“实际页码校准”填写一个对应关系，例如“PDF 第 527 页对应课本第 488 页”。
2. 选择教材并执行“建立 / 更新索引”。无文本图片页会自动调用 OCR。
3. 已有标准数据可直接选择 JSON 或 Excel；杂乱题目可点击“智能整理题目”，粘贴文本或读取 TXT、Markdown、DOCX。
4. 导入后启动解析任务，在“任务中心”查看进度。
5. 默认自动校验满足严格条件的 A 级证据题，异常题在审核工作台人工复核。自动校验不等于独立验证医学结论；可关闭自动审核。
6. 在“导出中心”同时生成 MedLearning v2 JSON 与五工作表 XLSX；解析映射仅作为旧工具兼容文件。

题目至少需要 `id`、`subject`、`question`、`answer`；选择题还需要两个以上 `options`。v2 未知字段会放入 `extensions` 保真保存。`explanationBlocks` 是解析正文权威来源，`knowledgePoints`、`tags`、`suggestedTags` 各最多 3 条。

## 数据位置

- 数据库：`%LOCALAPPDATA%\MedExplainStudio\med_explain.db`
- 日志与临时目录：`%LOCALAPPDATA%\MedExplainStudio`
- 默认导出：`%USERPROFILE%\Documents\医学题库解析`

卸载程序默认保留上述业务数据。

## 自动化 CLI

桌面端和 CLI 共用同一数据库及业务服务：

```powershell
python main.py library-add --name "内科学教材" --subject "内科学" --path "D:\教材\内科学.pdf" --page-offset 39
python main.py scan --library-id 1
python main.py import --questions "D:\题库\内科学.json" --name "内科学题库"
python main.py generate --set-id "题目集ID"
python main.py report --set-id "题目集ID"
python main.py export --set-id "题目集ID" --output-dir ".\output"
```

## 测试与打包

后续安装包固定采用 **轻量 EXE 启动器 + 同目录 BIN 数据包**，保留离线模型。交付与验收要求见 [Windows 打包与交付规范](docs/PACKAGING.md)。

```powershell
python tools/check_public_source.py
python -m pytest
cd frontend
npm test
npm run build
cd ..
.\build_windows.ps1 -SkipLocalInstall
```

`build_windows.ps1` 运行 Python／前端测试和类型检查，将便携程序、分盘安装器、源码快照、模型清单及验收报告保存到独立的 `release/MedExplainStudio-<版本>-<时间戳>/`。示例使用 `-SkipLocalInstall`，只构建不安装；省略该参数会备份并原位升级本机程序，请谨慎使用。缺少独立网页项目时，相关跨项目测试会明确跳过。

公开仓库不包含模型文件，未准备模型时部分 PDF 集成测试无法运行；请先执行模型准备命令。日常测试不调用真实云模型服务。参与开发和敏感信息报告方式见 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [SECURITY.md](SECURITY.md)。

云端处理范围：教材候选文本发送至 DashScope 生成向量／重排；本地解析失败并使用云回退时页面图像发送至 Qwen OCR；题目和检索证据发送至所选模型服务。程序不发送遥测数据。
