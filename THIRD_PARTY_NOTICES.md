# 第三方组件与许可

本项目自有源码采用 **AGPL-3.0-only**。依赖和模型仍保留各自的版权与许可；本项目许可证不替代或重新许可第三方组件。

主要依赖与上游许可入口：

| 组件 | 许可说明与来源 |
| --- | --- |
| PyMuPDF / MuPDF | 开源 AGPL 或商业双许可；本项目开源构建采用 AGPL 路径。[官方说明](https://pymupdf.readthedocs.io/en/latest/about.html) |
| PySide6 / Qt | LGPL、GPL 或商业许可，具体取决于模块；Qt WebEngine 另含第三方组件。[官方许可清单](https://doc.qt.io/qtforpython-6/licenses.html) |
| Docling | MIT。[项目许可证](https://github.com/docling-project/docling/blob/main/LICENSE) |
| RapidOCR | Apache-2.0。[上游项目](https://github.com/RapidAI/RapidOCR) |
| Instructor | MIT。[上游项目](https://github.com/567-labs/instructor) |
| Vue / Pinia / Vite | 各包保留其上游 MIT 许可；具体版本见 frontend/package-lock.json。 |
| PDF.js | Apache-2.0。[上游项目](https://github.com/mozilla/pdf.js) |
| Inter 字体 | SIL Open Font License。[上游项目](https://github.com/rsms/inter) |
| Phosphor 图标 | MIT。[上游项目](https://github.com/phosphor-icons/vue) |
| 其他 Python / npm 依赖 | 以安装的对应版本许可证、NOTICE 和分发元数据为准。 |

仓库仅包含应用源码、配置占位符、合成测试材料和模型哈希清单，不包含依赖二进制、模型权重或历史安装包。模型下载由 `tools/prefetch_docling_models.py` 显式执行；代码许可不等于模型权重许可，应分别核查下载版本和随附许可。

如需再分发构建出的 EXE / BIN，发布者须核查完整依赖和模型清单，保留所有适用版权／许可通知，并履行 AGPL、GPL、LGPL 等适用的源码提供和其他义务。这里的摘要不是完整的二进制分发合规审计，也不意味着已完成所有第三方许可核查。
