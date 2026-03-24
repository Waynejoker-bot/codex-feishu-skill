# Codex Feishu Skill

统一版飞书 skill，覆盖：
- 用户 OAuth 授权
- 长期 `user_access_token` 刷新
- 读取 wiki / docx / sheet / bitable
- 写入 docx 文档
- 创建和操作多维表格
- 给文档或多维表格分享权限

## 仓库结构

```text
.
├── SKILL.md
├── references/
└── scripts/
```

## 安装方式

推荐放到 Codex 的技能目录：

```bash
mkdir -p "$HOME/.codex/skills"
cp -R ./codex-feishu-skill "$HOME/.codex/skills/feishu"
```

如果是直接克隆到技能目录，也可以：

```bash
git clone https://github.com/<your-account>/codex-feishu-skill.git "$HOME/.codex/skills/feishu"
```

## 必备环境变量

在 skill 根目录准备 `.env`：

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
```

## 第一次授权

生成授权链接：

```bash
python3 scripts/feishu_user_auth.py \
  --env-file .env \
  --redirect-uri 'https://your-redirect.example/callback' \
  --print-auth-url \
  --scope 'auth:user.id:read bitable:app base:record:create docx:document docx:document:create docx:document:readonly docx:document:write_only docx:document.block:convert'
```

用户授权后，用最终回跳 URL 换 token：

```bash
python3 scripts/feishu_user_auth.py \
  --env-file .env \
  --redirect-uri 'https://your-redirect.example/callback' \
  --exchange-redirect-url 'https://your-redirect.example/callback?code=...&state=...'
```

授权结果会保存在：

```text
./.user_auth.json
```

## 长期授权

后续默认依赖 `refresh_token` 静默刷新：

```bash
python3 scripts/feishu_user_auth.py --env-file .env --refresh
```

## 常用命令

读取飞书内容：

```bash
python3 scripts/feishu_read.py --env-file .env --url 'https://xxx.feishu.cn/wiki/...'
```

新建飞书文档：

```bash
python3 scripts/feishu_doc_writer.py --env-file .env --title '测试文档' --content '# Hello'
```

新建多维表格：

```bash
python3 scripts/feishu_bitable.py --env-file .env create-app --name '测试多维表格'
```

更多命令和排查方法见 [SKILL.md](./SKILL.md) 与 [references/onboarding-and-troubleshooting.md](./references/onboarding-and-troubleshooting.md)。
