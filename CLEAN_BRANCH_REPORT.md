# Material Upload Extraction Repair — Clean Branch

本分支只调整 Git history 拓扑。从指定 main 应用已批准、已完成卫生修正及测试的 source 最终 diff，未 cherry-pick 原提交，未重新执行功能测试或运行时 QA。

```text
BASE_HEAD = 74be617a38b0dd0fe094e5ff621c4d8e7188766c
SOURCE_SANITIZED_HEAD = 69134e0b46b2535745247ec141ddb8d3fb210de2
CLEAN_BRANCH = material-upload-extraction-repair-clean
CLEAN_BRANCH_HEAD = refs/heads/material-upload-extraction-repair-clean (本报告所在的唯一提交)
CLEAN_COMMIT_COUNT = 1

FINAL_TREE_EQUIVALENCE = PASS
FUNCTIONAL_CODE_CHANGED = NO

CLEAN_TREE_REAL_COMPANY_HITS = 0
CLEAN_TREE_REAL_CREDIT_HITS = 0
CLEAN_HISTORY_REAL_COMPANY_HITS = 0
CLEAN_HISTORY_REAL_CREDIT_HITS = 0

GIT_DIFF_CHECK = PASS
PYTHON_COMPILE_SMOKE = PASS
PYTHON_IMPORT_SMOKE = PASS

FULL_REGRESSION = NOT_RERUN / PREVIOUSLY_PASS_208_OF_208
REAL_UMI_INTEGRATION = NOT_RERUN / PREVIOUSLY_PASS
REAL_DOWSE_INTEGRATION = NOT_RERUN / PREVIOUSLY_PASS
DESKTOP_QA = NOT_RERUN / PREVIOUSLY_PASS
MOBILE_QA = NOT_RERUN / PREVIOUSLY_PASS
LARGE_DOCUMENT_SMOKE = NOT_RERUN

PRODUCTION_TOUCHED = NO
```

`CLEAN_BRANCH_HEAD` 指向本报告所在的唯一提交；精确 SHA 在提交后以 `git rev-parse HEAD` 取得，并随最终交付消息提供。报告内容不虚构包含自身内容的 commit hash。

## 内容等价证明

移植方式为：

```text
git diff --binary --full-index <BASE_HEAD> <SOURCE_SANITIZED_HEAD>
git apply --index --binary <final-diff.patch>
```

添加本报告之前，整个 clean index 的 Git root tree 与 source root tree 完全相同：

```text
SOURCE_SANITIZED_TREE = c9840f910ead3787d095dbd2c7bbe916ec7166b3
CLEAN_TREE_BEFORE_REPORT = c9840f910ead3787d095dbd2c7bbe916ec7166b3
```

source 相对 main 的全部 27 个 changed paths 均逐一比对 Git mode 与 blob SHA。最终额外核对完整 source 树的 104 个文件，源码、测试、fixture、README/docs、公开 OCR 文本及三张截图全部保持原 blob，不修改原修复报告。最终 clean 树唯一新增文件为 `CLEAN_BRANCH_REPORT.md`。

另单独核对以下 13 个功能文件，均与 source 逐文件完全相同：

- `mcp_v01/jty_materials/business.py`
- `mcp_v01/jty_materials/workflow.py`
- `mcp_v01/jty_materials/reextract.py`
- `mcp_v01/jty_materials/manager.py`
- `mcp_v01/cli.py`
- `mcp_v01/webui/uploads.py`
- `mcp_v01/webui/api.py`
- `mcp_v01/webui/app.py`
- `mcp_v01/webui/display.py`
- `mcp_v01/webui/service.py`
- `mcp_v01/webui/pages.py`
- `mcp_v01/webui/catalog.py`
- `mcp_v01/webui/static/app.js`

## 本轮验证范围

仅执行 `git diff --check`、staged/committed diff check、`git status`、内容/历史卫生比较和涉及的 19 个 Python 文件 compile/import smoke。导入时使用外部进程启动拦截器；外部进程启动尝试为 0，执行的测试数为 0，未启动 Umi、dowse、Playwright、HTTP server 或 manager。

208 项完整回归、真实 Umi/dowse integration、桌面/手机 QA 及大文档 smoke 本轮没有重新执行。此前 source 已完成 208/208 PASS，FAIL/ERROR/SKIP 均为 0，并通过真实 Umi、桌面和手机 QA；本轮凭严格 tree 等价复用该验证结论，不把它写成新执行的 PASS。历史验证详见保持原样的 `MATERIAL_UPLOAD_EXTRACTION_REPAIR_REPORT.md`。

## Git history 卫生

专门检查当前 clean tree 与 `origin/main..HEAD` 的新增提交树、提交消息及完整 binary diff，包含 UTF-8、NFKC/去空白变体；三张 PNG 保持 source 已验证的合成截图原 blob。指定真实企业名称和信用代码均为 0 命中；原生产成立日期也不出现在新增树/历史文本中。

```text
git rev-list --count origin/main..HEAD = 1
git merge-base origin/main HEAD = 74be617a38b0dd0fe094e5ff621c4d8e7188766c
HEAD^ = 74be617a38b0dd0fe094e5ff621c4d8e7188766c
```

source 功能实现、旧报告和卫生修正的三个提交不在 clean branch 的新增祖先链上。source commit 仅作为内容比较对象；没有修改、重写或删除旧分支，也没有 merge/rebase/force push。

## 本地证据与生产边界

轻量证据保存在 clean worktree 的 Git-ignored `runs/clean-branch/`：

- `sanitized-final.patch`：main 到 source 的最终 diff。
- `tree-equivalence-before-report.json`：完整 root tree SHA 一致证明。
- `python-smoke.json`：19 个 Python 文件 compile/import 及零进程启动记录。
- `audit-index.json`：提交前全树 blob/mode 与卫生比较。
- `audit-head.json`：最终提交树、单提交、merge-base 与历史卫生结果。

未读取或写入生产，未访问或重新识别生产 Material，未停服务、改 config、迁 schema 或部署。只正常 push 新 clean branch，保留旧分支，等待最终 review。
