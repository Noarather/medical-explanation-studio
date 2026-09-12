# 模型服务设置与索引修复

桌面工具支持 DeepSeek（OpenAI Chat Completions）、Claude（Anthropic Messages）、Gemini（generateContent）和自定义服务。自定义服务可以选择上述任一协议，用于其他模型或 API 中转。

## 使用

1. 打开设置 → 模型服务 / API，选择服务。
2. 填写服务商提供的 API URL、模型 ID 和 API Key。URL 可填版本根路径或对应完整接口；自定义网关的版本和前缀会保留。模型名称以服务商实际授权的名称为准。
3. 点击“测试模型连通性”。测试会使用当前尚未保存的内容进行一次短文本生成，显示结果和耗时，产生少量 API 用量。仅能说明这个模型当前可调用，不能保证之后所有任务均成功。
4. 保存后，新启动的题目整理、解析生成、知识点和记忆卡任务使用所选服务。进行中的任务沿用启动时配置。

各服务的 URL、模型和密钥分别保存。密钥放入 Windows 凭据管理器，设置接口只返回是否配置，不回传密钥。输入框留空沿用已存密钥；更换 URL 时需重新填写密钥。环境变量仍优先于凭据管理器：DEEPSEEK_API_KEY、ANTHROPIC_API_KEY、GEMINI_API_KEY、CUSTOM_LLM_API_KEY、DASHSCOPE_API_KEY。使用环境变量的部署需同步更新环境变量。

难题备用模型必须属于同一服务和 URL；非 DeepSeek 服务未填写备用模型时，只使用主模型，不自动调用 DeepSeek Pro。DeepSeek 专用 thinking 参数不会发送给其他服务。

教材向量、Rerank、云端 OCR 仍使用 DashScope，相关 URL 与模型在“向量 / OCR”页配置。DashScope 测试实际请求向量模型，并不代表 OCR 或 Rerank 的权限也已通过验证。

## 教材索引异常

“No module named 'docling' / 'rapidocr'”表示记录生成时的 Python 环境缺少解析组件，不是 API Key 错误。旧索引记录不会因软件升级自动变为成功。

在教材库点击“检查解析环境”，检查当前进程可找到的依赖和 OCR 模型文件。它不执行 PDF 解析，也不联网。安装版缺组件时用完整的 EXE + 全部 BIN 覆盖安装；源码版通过 run_desktop.ps1 补装依赖。随后对异常教材点击“重试异常索引”。有警告的旧索引会重试，重建前自动备份；正常且未变化的教材仍直接跳过。

OCR 页数统计明确由备用 OCR 处理的页面；Docling 内部使用 OCR 的页面没有单独页数标记，不能凭这个数字判断教材是否需要 OCR。

Windows 解析使用模型的常规推理模式，避免依赖本机编译器的 torch.compile 初始化。启动脚本与打包程序开启 UTF-8 模式，避免中文系统默认编码影响模型文件读取。公式识别和离线模型均保留。

## 开发与验证依据

- [Claude Messages API](https://platform.claude.com/docs/en/api/messages/create)
- [Gemini generateContent API](https://ai.google.dev/api/generate-content)
- [DeepSeek Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion)
- [PyInstaller 的 UTF-8 启动选项](https://pyinstaller.org/en/latest/spec-files.html#specifying-python-interpreter-options)

HTTP 适配测试使用本地 MockTransport，不消耗用户的 API 额度。发布继续遵守 docs/PACKAGING.md 的轻量 EXE + 外置 BIN 方案，保留离线模型。
