# 飞书接入与异常排查手册

这份文档的目标不是解释概念，而是让人按顺序排查，快速把事情做通。

## 一、先判断你要用哪种身份

默认优先用用户身份。

用用户身份：
- 用户要求“以我的身份”读写
- 需要把记录、文档、更新动作落到用户名下
- 用户自己本来就能在飞书界面里访问这个资源

用应用身份：
- 需要创建应用自有资源
- 做后台任务、批处理、自动化同步
- 用户身份权限还没配好，但业务允许先用应用身份

重要区别：
- 飞书界面里的“你能编辑”不等于“你的 `user_access_token` 有 API scope”
- 资源权限和 OAuth scope 是两层校验，缺任何一层都会失败

## 二、首次接入清单

### 1. 创建或确认飞书应用

这里通常就是你说的“机器人”或“自建应用”。

至少要确认：
- 有 `App ID`
- 有 `App Secret`
- 这两个值已经放进 `.env`

建议 `.env` 里至少有：

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
```

### 2. 配置重定向地址

在飞书开放平台后台，把实际要接收 OAuth 回跳的地址加入白名单。

常见两种：
- 本地回调：`http://127.0.0.1:19876/callback`
- 飞书页面回跳：例如某个 wiki 页面 URL

如果不是本地回调，就不要等脚本自动监听本地端口，而是用：
- `--exchange-redirect-url`

把最终跳转后的完整 URL 粘回来换 token。

### 3. 申请权限

一定要分清：
- 应用身份权限 `tenant_access_token`
- 用户身份权限 `user_access_token`

如果要以用户身份操作，就必须看“用户身份权限”这一栏。

常见权限：

最小用户身份：
- `auth:user.id:read`

多维表格写入：
- `bitable:app`
- `base:record:create`

多维表格完整 CRUD：
- `bitable:app`
- `base:record:create`
- `base:record:read`
- `base:record:update`
- `base:record:delete`

文档 docx 读写：
- `docx:document`
- `docx:document:create`
- `docx:document:readonly`
- `docx:document:write_only`
- `docx:document.block:convert`

如果还想通过 shortcut API 自动拿到文档 URL，再补：
- `drive:drive`
- `space:document:shortcut`

分享权限：
- `drive:permission`

读 Sheet 常见还需要：
- `sheets:spreadsheet:readonly`
- `drive:drive:readonly`

### 4. 发布版本

只勾权限不够，必须发布。

任何权限变更之后都默认做这件事：
- 保存
- 发布最新版本

不发布时，最常见现象就是：
- 后台看起来已经勾上了
- 但新 token 仍然拿不到对应 scope

### 5. 重新授权

旧 token 不会自动升级到新 scope。

只要权限发生变化，就要重新走一次授权。

关键点：
- 授权链接里必须显式带 `scope`
- 不能只指望“后台勾了权限”

## 三、正确的用户授权方式

### 1. 先生成带 scope 的授权链接

```bash
python3 scripts/feishu_user_auth.py \
  --env-file /absolute/path/to/.env \
  --redirect-uri 'https://your-redirect.example/callback' \
  --print-auth-url \
  --scope 'auth:user.id:read bitable:app base:record:create'
```

注意：
- `scope` 是空格分隔
- 少一个就可能少一层能力

### 2. 用户授权完成后，用完整回跳 URL 换 token

```bash
python3 scripts/feishu_user_auth.py \
  --env-file /absolute/path/to/.env \
  --redirect-uri 'https://your-redirect.example/callback' \
  --exchange-redirect-url 'https://your-redirect.example/callback?code=...&state=...'
```

### 3. 后续长期使用 refresh_token

```bash
python3 scripts/feishu_user_auth.py \
  --env-file /absolute/path/to/.env \
  --redirect-uri 'https://your-redirect.example/callback' \
  --refresh
```

结论：
- `code` 是一次性的
- `refresh_token` 才是长期复用的

## 四、长期授权的正确理解

“长期授权”不是拿一个永不过期的 access token。

正确做法是：
1. 第一次人工授权，拿到 `access_token + refresh_token`
2. 把 `refresh_token` 保存到本地
3. 每次用前自动刷新，拿到新的 `access_token`
4. 刷新时如果飞书轮转了新的 `refresh_token`，要立刻覆盖保存

注意：
- 刷新成功后，旧 `access_token` 可能立刻失效
- 旧 `refresh_token` 也可能被飞书立刻作废

所以如果一个流程里刷新过 token，后续所有请求都要切到新 token。

## 五、资源权限与 OAuth scope 的关系

很多人会混淆这两层：

第一层：资源权限
- 你有没有被分享这个文档/多维表格
- 你在飞书界面里能不能打开、编辑

第二层：OAuth scope
- `user_access_token` 有没有被授予调用这个 API 的权限

结论：
- 两层都要有
- 少任意一层都会失败

典型现象：
- 飞书里能编辑，但 API 报 `99991679`
  说明资源权限有了，但 token scope 没有

## 六、标准排查顺序

以后遇到异常，不要随机尝试，按下面顺序查：

1. 看自己在用的是用户身份还是应用身份
2. 看报错里缺的具体 scope 名称
3. 去后台确认对应的是“用户身份权限”还是“应用身份权限”
4. 确认权限已经开通
5. 确认权限变更后已经发布
6. 确认授权链接里显式带了这些 scope
7. 重新授权拿新 token
8. 如果刚刷新过 token，确认后续请求用的是最新 token
9. 如果是应用自有资源，确认已经分享给目标用户

## 七、常见错误与处理

### 1. `99991679`

含义：
- 当前 `user_access_token` 没有目标 API 所需权限

处理：
1. 看错误里列出的缺失权限
2. 去后台开通对应权限
3. 发布版本
4. 重新授权，并且授权链接里显式带这些 scope

官方参考：
- https://open.feishu.cn/document/uAjLw4CM/ugTN1YjL4UTN24CO1UjN/trouble-shooting/how-to-resolve-error-99991679.md

### 2. `99991668`

常见原因：
- token 无效
- 你刚刷新过 token，但后续请求还在拿旧 token

处理：
1. 刷新一次 token
2. 从缓存文件里重新读最新 token
3. 后续请求全部改用新 token

### 3. 后台明明勾了权限，但 scope 还是没下来

优先查这三件事：
1. 权限有没有发布
2. 授权链接有没有显式带 `scope`
3. 有没有重新授权

这三件只要缺一个，都可能导致新 token 还是旧能力。

### 4. 用户在 UI 能编辑，但 API 写不进去

原因：
- UI 编辑权不等于 API scope

处理：
- 继续查用户身份权限
- 继续查授权链接里的 scope

### 5. 多维表格是应用创建的，用户写不进去

原因：
- 资源 owner 是应用
- 用户虽然有 token，但没有该资源权限

处理：
1. 先用应用身份把 bitable 分享给目标用户
2. 再切回用户身份写入

### 6. Sheet 读取失败

经验上常见缺：
- `sheets:spreadsheet:readonly`
- `drive:drive:readonly`

即使你不是在“下载文件”，飞书依然可能按 Drive 资源做权限校验。

## 八、建议的最小验证命令

先验证用户 token 是否可刷新：

```bash
python3 scripts/feishu_user_auth.py \
  --env-file /absolute/path/to/.env \
  --redirect-uri 'https://your-redirect.example/callback' \
  --refresh
```

再验证是否能读取目标资源：

```bash
python3 scripts/feishu_read.py \
  --env-file /absolute/path/to/.env \
  --url 'https://...'
```

再验证是否能写 bitable：

```bash
python3 scripts/feishu_bitable.py \
  --env-file /absolute/path/to/.env \
  create-record \
  --app-token appcnxxxxxxxx \
  --table-id tblxxxxxxxx \
  --field '测试字段=测试值'
```

最后再做批量导入、批量删除、文档覆盖写入。

## 九、推荐心法

飞书这类问题不要把“后台看起来配了”当成完成。

真正的完成标准只有三个：
- 新 token 已经拿到
- 实际返回的 scope 正确
- 目标 API 真实调用成功
