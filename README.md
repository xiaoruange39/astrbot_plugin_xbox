![:name](https://count.getloli.com/@astrbot_plugin_xbox?name=astrbot_plugin_xbox&theme=booru-r6gdrawfriends&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# Xbox Game Pass 入库提醒插件 🎮

获取微软 Xbox Game Pass 最新入库游戏列表，支持后台定时推送新游提醒和手动查询。

![XGP Preview](https://picui.ogmua.cn/s1/2026/03/15/69b6c6c6a6635.webp) 

## ✨ 特性

- **精美海报生成**：自动生成包含游戏封面、平台信息、订阅等级（ULTIMATE/PREMIUM）的高清合集海报。
- **灵活定时推送**：支持标准 Cron 表达式设置，仅在有新游入库时才打扰。

## 🚀 安装

1. 在 AstrBot 管理面板中，进入“插件”页面。
2. 点击右上角的“从 GitHub 安装”或上传本插件文件夹。
3. 仓库地址：`https://github.com/xiaoruange39/astrbot_plugin_xbox`
4. 安装完成后，点击“重载插件”。

## ⚙️ 配置项说明

在管理面板找到本插件，点击“配置”：

- **显示游戏数量**：设置每次手动查询或定时推送时显示的游戏海报上限（共 1-36 个）。
- **定时推送时间 (Cron)**：设置自动推送频率，如 `0 10 * * *` 表示每天 10:00 推送。
- **推送目标列表 (UMO)**：填入接收者的 UMO ID。
- **仅在发现更新时推送**：开启后，若当日无新游戏入库则不发送推送消息。
- **显示加载提示**：执行指令时是否先回显一条等待消息。

## 🛠️ 指令列表

- `/xgp`：立即查询并生成最近入库的 Game Pass 游戏海报。

## 📄 申明

- 本插件数据来源于微软官方公开接口。
- 本插件仅供学习交流使用。

---

- **作者**: [xiaoruange39](https://github.com/xiaoruange39)
- **QQ群**: [QQ群](https://qm.qq.com/q/8kdJ2Bzf6S)
