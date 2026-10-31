# vaultwarden-templates-zh

**非官方**的 [Vaultwarden](https://github.com/dani-garcia/vaultwarden) 邮件模板简体中文翻译，并带一个自动跟随官方 release 的同步脚本。与 Vaultwarden、Bitwarden 项目均无关联。

当前对应的官方版本见 [`VERSION`](VERSION)。模板版本最好和你运行的镜像版本一致。

## 使用

邮件模板放在 `templates/email/`。Vaultwarden 默认从 `数据目录/templates` 读取自定义模板，同名文件会覆盖内置英文版，所以**不需要设置任何环境变量**。

```bash
# 以 docker compose 里挂载了 ./data:/data 为例
git clone https://github.com/isalikai/vaultwarden-templates-zh.git zh-templates
mkdir -p data/templates
cp -r zh-templates/templates/email data/templates/
docker compose restart vaultwarden     # 默认不热加载模板，需要重启
```

- 在 `/admin` 的 SMTP 设置处发一封测试邮件，或触发一次新设备登录，确认邮件是中文。
- 想恢复英文：删掉 `data/templates/email/` 里对应文件（或整个目录）再重启即可。
- 升级 Vaultwarden 前，先合并本仓库对应版本的同步 PR，再 `git pull` 并复制新模板，最后升级镜像。

## 它是怎么翻译的

模板里 90% 是内联 CSS 和 Handlebars 语法，真正要翻译的文字很少。脚本只翻译**文字**：

- `{{变量}}`、`{{#if}}`、HTML 标签、注释、`<style>`、链接、HTML 实体先被替换成 `⟦1⟧` 这样的令牌，翻译后必须原样、各出现一次，否则该句拒收并保留英文。
- 译文只替换进官方原文的对应位置，其余内容逐字节保持不变。
- 整个文件再做一次校验：所有 `{{...}}`、标签、链接必须与官方原文一致，否则整份文件回退为英文。
- 译文按句子存放在 [`translations.json`](translations.json)（翻译记忆库）。相同句子永远译法一致，重复运行只翻译新增或变化的句子，不满意可以直接改这个文件。

## 仓库结构

```
VERSION                当前对应的官方版本
templates/email/       翻译后的模板（部署用这个）
upstream/email/        官方原文快照（用来在 PR 里看官方改了什么）
translations.json      句子级翻译记忆库，可手工修改
glossary.json          术语表，翻译时作为提示词
scripts/sync.py        同步 + 翻译 + 校验脚本（仅用标准库）
tests/                 单元测试
.github/workflows/     sync.yml（每日同步并提 PR）、test.yml
```

## 已知限制

- 变量的值由 Vaultwarden 生成，不会被翻译：设备类型（如 Android）、日期时间格式，以及紧急访问邮件里的操作类型 `View` / `Takeover`（译文里写作“执行“View”操作”）。
- “设置 > 我的账户 > 取消授权会话”是网页版密码库的菜单路径，译文按 Bitwarden 中文界面的常见叫法写成，如果你的网页版语言不同，请改 `translations.json` 里对应句子。
- 官方新增了模板文件而这里还没同步时，Vaultwarden 会继续使用内置英文版，不会出错。

## 许可与声明

模板派生自 Vaultwarden（AGPL-3.0），本仓库同样以 [AGPL-3.0](LICENSE) 发布。邮件内容里的 GitHub 链接指向官方项目。
