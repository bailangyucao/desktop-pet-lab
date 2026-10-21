# 工具与第三方依赖

本仓库提交自有代码、测试和文档，未捆绑 Python、Tcl/Tk、Pillow、PyInstaller 的运行库副本。安装依赖或生成可执行程序时，会使用相应上游软件；应保留实际版本附带的许可与版权说明。根目录 MIT 不替代第三方许可。

| 软件 | 用途 | 版本范围或本地验证版本 | 上游 |
| --- | --- | --- | --- |
| Python | 运行时 | 3.12；本地 3.12.14 | [python.org](https://www.python.org/) |
| Tcl/Tk | 原生桌面界面 | Python 自带；本地 Tk 8.6.12 | [Tcl/Tk](https://www.tcl.tk/) |
| Pillow | 精灵图处理 | >=11.3,<13；本地12.3.0 | [Pillow](https://github.com/python-pillow/Pillow) |
| PyInstaller | 可选 Windows EXE 构建 | >=6.19,<7；本地6.19.0 | [PyInstaller](https://github.com/pyinstaller/pyinstaller) |

requirements.txt 是安装范围，不是逐版本锁定文件；这里记录的本地版本用于说明验证环境。

开发工具使用 Codex，模型协议参考 OpenAI Chat Completions 及 SenseNova 官方 API 文档，具体记录见 [致谢](ACKNOWLEDGEMENTS.md)。没有把这些工具或模型服务称为代码作者或项目担保方。

角色图片与图标不在公开包中，来源状态见 [素材说明](ASSET_PROVENANCE.md)。
