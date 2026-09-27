# ccodex-sleep-plus

Codex `X-Codex-Turn-State` 本地网关 + 系统托盘常驻 + 独立面板窗口。[gylive/ccodex-sleep-state](https://github.com/gylive/ccodex-sleep-state) 社区思路的独立增强实现。

> 免责：turn-state 的块数/长度规则是社区经验，**不是官方指标，不保证任何效果**；探测会消耗真实额度。

## 社区调研结论（2026-09-27）

- **33 块（780 字符）是 2026-09-22 后的官方新形状**（[原项目 issue #13](https://github.com/gylive/ccodex-sleep-state/issues/13)），不是降智标记；本工具同时接纳 10/12/33 块
- **降智由上游按"账号 × 出口 IP × 分钟级时间窗"裁决**（[ccodex-rotate](https://github.com/446599/ccodex-rotate) 的行为归因结论），与 state 形状无关；**换出口节点才是主要杠杆**
- 注入窗口可能只有约 240 秒（同上），长 TTL 注入的实际收益有限——本工具默认兜底转发，不赌注入
- `response.created.model`（served 字段）可能失真，效果观测里的 token 用量更可靠
- 质量判定采用 [csss](https://github.com/tzf1003/csss) 的知识新鲜度探针：答 iPhone 17 = 满血 / 16 = 降智 / 15 = 严重降智（规则随时间过时，随社区更新）

## 特性

- **零依赖核心**：网关/采集/配置接管仅用 Python 标准库（3.14+），代码可审计
- **系统托盘常驻**：后台服务 + 托盘图标（DPI 感知，菜单清晰），面板从托盘右键打开，关面板窗口不停服务
- **独立面板窗口**：pywebview（Edge WebView2）独立进程，不占用浏览器；圆角卡片、TTL 倒计时环、出口健康、实时脱敏日志、**效果观测对比**（注入开/关时服务端自报模型与用量）
- **官方新形状支持**：10/12 块（292/332）+ 33 块（780）新形状（issue #13，2026-09-22 官方扩展）
- **出口质量体检**：知识新鲜度探针（iPhone 档位）实测每个出口是否被降智 serving，面板一键体检；采集探针与质量探针合并，一次请求两用
- **多实例 state 池**：active + 3 个备用实例，指纹去重、自动轮换
- **auth.json 自举**：启动即采集，无需先在 Codex 发消息
- **默认不拦截**：采不到合格 state 时普通转发兜底（可切严格模式），正式请求最多转发一次、绝不重放
- **响应不销毁**：注入请求响应的 state 形状异常只记 strike 并切换备用，不丢弃已计费回复（原版会拦掉正文）
- **指数退避**：连续采不到合格 state 时 3→10→30→60 分钟退避，避免烧额度；采到即重置
- **state 落盘**：重启不丢；密钥/设置稳定持久
- **令牌解封**：codex 刷新令牌后自动解除 401 封锁
- **出口自发现**：系统代理注册表 / 常见本地端口（7897/7890/10808…）/ 环境变量，HTTP CONNECT 与 SOCKS5 均支持，出口健康检测（仅 TLS 握手，不耗额度）
- **网关监督**：转发线程崩溃自动重启；桌面版重写 config.toml 丢失接管时 30 秒自动补回
- **安装包**：exe 双击安装，自动识别 CODEX_HOME、检测代理出口、创建桌面/开始菜单快捷方式、可选开机自启

## 安装（Windows）

下载 Release 的 `ccodex-sleep-plus.exe`，双击：

1. 自动识别 `~/.codex`（或 `CODEX_HOME`）与登录状态
2. 自动检测本机代理出口（握手测试，不发请求、不耗额度）
3. 安装到 `%LOCALAPPDATA%\Programs\ccodex-sleep-plus`，创建桌面 + 开始菜单快捷方式
4. 启动托盘服务并打开面板

**之后重启 Codex（桌面版/CLI）使接管生效。** 卸载：`ccodex-sleep-plus.exe uninstall`。

## 源码运行

```powershell
pip install pystray pillow pywebview   # 托盘与面板窗口（网关核心零依赖）
python sleep_plus.py install           # 安装器：识别 + 快捷方式 + 启动
python sleep_plus.py tray              # 仅启动托盘服务
python sleep_plus.py probe             # 单次试采（消耗一次极短请求）
python sleep_plus.py stop              # 停止并恢复 Codex 配置
python test_gateway.py                 # 集成测试（mock 上游，零额度）
python sleep_plus.py selftest          # 单元自检
```

## 机制与边界（与社区共识一致）

- 只处理 Responses HTTP/SSE；不支持 WebSocket
- state 封装校验：`0x80` 头 + 8 字节大端时间戳 + 48 字节固定头 + N×16 字节密文块，base64url；TTL 1 小时，临期 20 分钟自动续采
- 采集请求体与原版一致（"Reply with OK."，`include: reasoning.encrypted_content`）
- 401/403 封锁凭据、429 按 Retry-After 暂停；不通过换出口绕过上游限制
- 认证值与完整 state 不写日志；面板只在本机回环可访问
- 代理出口支持 HTTP CONNECT / SOCKS5（纯标准库实现）

## 文件

- `sleep_plus.py` — 全部逻辑（网关 / 采集 / 托盘 / 面板 / 安装器）
- `test_gateway.py` — 集成测试（mock 上游，零额度）
- `build_exe.py` — PyInstaller 打包脚本
- `start.cmd` / `stop.cmd` — 源码模式启动/停止

## 致谢与许可

思路与经验规则来自 [gylive/ccodex-sleep-state](https://github.com/gylive/ccodex-sleep-state)（GPL-3.0）及其社区讨论（含 issue #13 的新形状反馈），macOS 同类工具 [zzusec/CheckClaude](https://github.com/zzusec/CheckClaude) 的跨会话缓存思路亦值得参考。

GPL-3.0，见 [LICENSE](LICENSE)。本项目与 OpenAI 无隶属关系。
