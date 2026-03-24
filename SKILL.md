---
name: feishu
description: Use when 处理飞书 wiki、文档、表格、多维表格、权限分享，或排查 Feishu、飞书、飞书授权 OAuth、user_access_token、scope、99991679、redirect_uri、长期授权等问题。
---

# 飞书统一 Skill

这是统一版飞书 skill，覆盖：
- 用户 OAuth 授权
- 长期 user token 刷新
- 读取 wiki / docx / sheet / bitable
- 写入 docx 文档
- 创建和操作多维表格
- 给文档或多维表格加协作者权限

## 什么时候用

只要任务里出现这些场景，就用这个 skill：
- 飞书链接、飞书 token、飞书文档、飞书 wiki、飞书 sheet、多维表格
- “以我的身份”调用飞书 API
- 给飞书文档/多维表格分享权限
- `99991679`、缺 scope、redirect_uri、授权码、refresh_token、长期授权

默认规则：
- 读写默认都走 `auth-mode=user`
- 只有在明确需要操作应用自有资源，或用户权限不适用时，才切到 `auth-mode=app`

## 四条硬规则

1. `authorization_code` 只能用一次，真正可复用的是 `refresh_token`
2. 遇到 `99991679` 时，不要只看后台是否勾了权限，还要重新授权并显式传 `scope`
3. 飞书浏览器 OAuth 链接必须显式带 `scope`
4. 刷新 user token 之后，继续操作时必须用“新的 token”，不要再拿刷新前的旧 token

官方参考：
- [获取授权码](https://open.feishu.cn/document/common-capabilities/sso/api/obtain-oauth-code.md)
- [浏览器网页接入指南](https://open.feishu.cn/document/common-capabilities/sso/web-application-end-user-consent/guide.md)
- [99991679 排查](https://open.feishu.cn/document/uAjLw4CM/ugTN1YjL4UTN24CO1UjN/trouble-shooting/how-to-resolve-error-99991679.md)

首次接入、权限申请、授权方式、异常排查的完整手册见：
- `references/onboarding-and-troubleshooting.md`

## 常用 Scope 组合

所有 scope 都是“空格分隔”。

最小用户身份：
- `auth:user.id:read`

多维表格用户写入：
- `bitable:app`
- `base:record:create`

多维表格完整 CRUD：
- `bitable:app`
- `base:record:create`
- `base:record:read`
- `base:record:update`
- `base:record:delete`

飞书文档 docx 读写：
- `docx:document`
- `docx:document:create`
- `docx:document:readonly`
- `docx:document:write_only`
- `docx:document.block:convert`

如果希望通过 shortcut API 获取更稳定的文档 URL，再补：
- `drive:drive`
- `space:document:shortcut`

权限分享：
- `drive:permission`

## 授权工作流

统一用：
- `scripts/feishu_user_auth.py`

生成授权链接：

```bash
python3 scripts/feishu_user_auth.py \
  --env-file /absolute/path/to/.env \
  --redirect-uri 'https://your-redirect.example/callback' \
  --print-auth-url \
  --scope 'auth:user.id:read bitable:app base:record:create'
```

用回跳 URL 换 token：

```bash
python3 scripts/feishu_user_auth.py \
  --env-file /absolute/path/to/.env \
  --redirect-uri 'https://your-redirect.example/callback' \
  --exchange-redirect-url 'https://your-redirect.example/callback?code=...&state=...'
```

后续静默刷新：

```bash
python3 scripts/feishu_user_auth.py \
  --env-file /absolute/path/to/.env \
  --redirect-uri 'https://your-redirect.example/callback' \
  --refresh
```

统一用户授权缓存文件：
- `./.user_auth.json`

## 读取飞书内容

统一用：
- `scripts/feishu_read.py`

读任意支持的飞书 URL：

```bash
python3 scripts/feishu_read.py \
  --env-file /absolute/path/to/.env \
  --url 'https://acnhb5kgvgtx.feishu.cn/wiki/...'
```

当前支持：
- `wiki`
- `docx`
- `sheet`
- `bitable`

## 写入飞书文档

统一用：
- `scripts/feishu_doc_writer.py`

新建文档：

```bash
python3 scripts/feishu_doc_writer.py \
  --env-file /absolute/path/to/.env \
  --title '项目周报' \
  --content-file /absolute/path/to/report.md \
  --json
```

覆盖已有文档：

```bash
python3 scripts/feishu_doc_writer.py \
  --env-file /absolute/path/to/.env \
  --document-id doxcxxxxxxxxxxxxxxxxxxxxx \
  --content-file /absolute/path/to/report.md \
  --replace-document \
  --json
```

默认优先按用户身份写。
只有在没有可用用户授权文件时，才回退到应用身份。

## 多维表格与权限操作

统一用：
- `scripts/feishu_bitable.py`

列出表：

```bash
python3 scripts/feishu_bitable.py \
  --env-file /absolute/path/to/.env \
  list-tables \
  --app-token appcnxxxxxxxx
```

以用户身份写一条记录：

```bash
python3 scripts/feishu_bitable.py \
  --env-file /absolute/path/to/.env \
  --auth-mode user \
  create-record \
  --app-token appcnxxxxxxxx \
  --table-id tblxxxxxxxx \
  --field '文章=标题' \
  --field '链接=https://example.com'
```

给文档或多维表格加编辑权限：

```bash
python3 scripts/feishu_bitable.py \
  --env-file /absolute/path/to/.env \
  --auth-mode app \
  share-member \
  --token appcnxxxxxxxx \
  --type bitable \
  --member-type openid \
  --member-id ou_xxxxx \
  --perm edit
```

## 高概率踩坑点

后台明明勾了权限，但新 token 还是没有：
- 授权链接里大概率没带 `scope`
- 或者权限改了但版本没发布

`99991679`：
- 先看缺哪个 scope
- 再确认后台已开通并发布
- 最后重新授权，并把缺失 scope 显式写进授权链接

刷新后立刻出现 `99991668`：
- 旧 access token 已经失效
- 继续后续请求时，要切到新的 token

回调不是 localhost：
- 不要硬等本地回调
- 直接用 `--exchange-redirect-url` 把最终跳转 URL 整条喂回来

用户态写不动，但应用态可以：
- 先把应用自有文档/多维表格分享给用户
- 再切 `auth-mode=user`

如果问题比较复杂，不要边试边猜，直接按排查手册从上到下核对一次。
